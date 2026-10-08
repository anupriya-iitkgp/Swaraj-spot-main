"""Provisioning / Hypervisor — edge 12, out of scope (HLD §1).

LLD §9:

    create, deliver_notice, wait_for_clean_exit, force_stop, teardown, destroy
    All idempotent; teardown confirms volumes and IPs released.
    Behaviour if it breaks: escalate to destroy; quarantine host.

One deliberate departure from that operation list, and it is the most important
design decision in this rewrite.

`wait_for_clean_exit` is **not** part of this interface. It is a blocking wait,
and a blocking wait needs something in this process to be doing the waiting —
which is precisely the in-memory `asyncio.Task` that LLD §12.3 identifies as the
worst gap in the design:

    "Grace timers are in-memory asyncio.Tasks with no persistence. A restart
    during a grace window strands the lease in NOTICE_ISSUED forever: never
    stopped, never billed, capacity never returned."

You cannot fix that by persisting the timer alongside the wait; as long as the
authoritative clock lives in a coroutine, a restart kills it. So the wait is
inverted. The control plane issues the notice, persists an absolute
`force_stop_deadline`, and returns. A guest that exits cleanly is reported *to*
the control plane (edge 13 becomes a real callback). A guest that does not is
picked up by the database-backed reaper on its next tick.

The result is that there is no in-process timer anywhere on the preemption path,
so restart-safety is a property of the design rather than a recovery procedure.
The simulator below does own timers — but it is simulating the *guest*, which
genuinely is an external process, and that is exactly where the waiting belongs.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Any, Awaitable, Callable, Protocol, Sequence

import httpx

from ..config import Settings
from ..domain.models import utcnow
from ..logging import edge, get_logger
from .base import ExternalCaller, ExternalError

log = get_logger(__name__)

__all__ = [
    "Hypervisor",
    "SimulatedHypervisor",
    "HttpHypervisor",
    "HostUnreachable",
    "GuestBehaviour",
]


class HostUnreachable(ExternalError):
    """The host agent did not answer a force stop.

    LLD §11 pairs this with two actions: "escalate to hypervisor destroy" and
    "quarantine host from the spot pool".
    """

    def __init__(self, host_group: str, operation: str) -> None:
        super().__init__(
            "hypervisor",
            operation,
            f"host agent on {host_group} is unreachable",
            retryable=False,
        )
        self.host_group = host_group


class GuestBehaviour:
    """How a simulated guest responds to a preemption notice."""

    COOPERATIVE = "cooperative"      # exits cleanly, well inside the window
    SLOW = "slow"                    # exits, but only just in time
    IGNORES_NOTICE = "ignores"       # never exits; must be force-stopped
    HOST_UNREACHABLE = "unreachable" # force stop fails; escalate and quarantine
    #: Stops, but volumes/IPs miss the budget. Resolves after a couple of sweeps,
    #: which is what a real stall usually does — a slow volume detach, not a dead
    #: host. The sweeper's job is to get the capacity back without an operator.
    TEARDOWN_STALLS = "stalls"
    #: Never releases. Models the case that genuinely needs a human: units stay
    #: RECLAIMING indefinitely rather than being falsely reported free.
    TEARDOWN_STALLS_FOREVER = "stalls_forever"


class Hypervisor(Protocol):
    async def create(
        self, *, lease_id: str, host_group: str, flavour: str, count: int
    ) -> list[str]: ...

    async def deliver_notice(
        self, *, lease_id: str, instance_ids: Sequence[str], deadline: datetime
    ) -> bool: ...

    async def force_stop(
        self, *, lease_id: str, host_group: str, instance_ids: Sequence[str]
    ) -> None: ...

    async def teardown(
        self, *, lease_id: str, instance_ids: Sequence[str], budget_seconds: float
    ) -> bool: ...

    async def destroy(
        self, *, lease_id: str, instance_ids: Sequence[str]
    ) -> None: ...


class SimulatedHypervisor:
    """A guest population that behaves like a real one.

    Behaviour is assigned deterministically from the lease id, so a given lease
    always behaves the same way across runs — a failure in the reclaim path is
    reproducible rather than a flake. `pin_behaviour` overrides it for tests
    that need a specific case.

    Roughly a quarter of guests do not honour the notice. That is not
    pessimism; it is the condition HLD §11 sizes the design around when it makes
    the timer authoritative and requires 99.9% reclaim completion regardless of
    what the guest does.
    """

    #: Cumulative weights. Tuned so every branch of the reclaim path is
    #: exercised by an ordinary workload rather than only by fault injection.
    _WEIGHTS: tuple[tuple[str, float], ...] = (
        (GuestBehaviour.COOPERATIVE, 0.68),
        (GuestBehaviour.SLOW, 0.86),
        (GuestBehaviour.IGNORES_NOTICE, 0.96),
        (GuestBehaviour.TEARDOWN_STALLS, 0.99),
        (GuestBehaviour.HOST_UNREACHABLE, 1.00),
    )

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._instances: dict[str, list[str]] = {}
        self._stopped: set[str] = set()
        self._destroyed: set[str] = set()
        self._pinned: dict[str, str] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._on_clean_exit: Callable[[str], Awaitable[None]] | None = None
        self._teardown_attempts: dict[str, int] = {}

    # -- wiring ------------------------------------------------------------
    def set_clean_exit_callback(
        self, callback: Callable[[str], Awaitable[None]]
    ) -> None:
        """Register edge 13: the guest telling us it has exited.

        In production this arrives as an authenticated call to
        `POST /internal/spot/leases/{id}/exited` from the host agent. Here the
        simulator calls it directly.
        """
        self._on_clean_exit = callback

    def pin_behaviour(self, lease_id: str, behaviour: str) -> None:
        self._pinned[lease_id] = behaviour

    def behaviour_for(self, lease_id: str) -> str:
        if lease_id in self._pinned:
            return self._pinned[lease_id]
        digest = hashlib.sha256(lease_id.encode()).digest()
        draw = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        for behaviour, threshold in self._WEIGHTS:
            if draw < threshold:
                return behaviour
        return GuestBehaviour.COOPERATIVE

    async def aclose(self) -> None:
        """Cancel outstanding guest simulations at shutdown."""
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- operations --------------------------------------------------------
    async def create(
        self, *, lease_id: str, host_group: str, flavour: str, count: int
    ) -> list[str]:
        """Idempotent: a retry returns the instance ids already created."""
        if lease_id in self._instances:
            return list(self._instances[lease_id])
        ids = [f"i-{lease_id.split('-')[-1][:12]}-{n}" for n in range(count)]
        self._instances[lease_id] = ids
        edge(
            log, 12, f"created {count} instance(s) on {host_group}",
            lease_id=lease_id, host_group=host_group, flavour=flavour,
            instance_ids=ids,
        )
        return list(ids)

    async def deliver_notice(
        self, *, lease_id: str, instance_ids: Sequence[str], deadline: datetime
    ) -> bool:
        """Write the notice to the guest-local metadata service.

        Returns whether the metadata channel accepted it. This is one of the
        three independent channels in HLD §6; the other two are delivered by
        `core.notice_delivery` over entirely different infrastructure.
        """
        behaviour = self.behaviour_for(lease_id)
        if behaviour == GuestBehaviour.HOST_UNREACHABLE:
            # A host we cannot reach cannot be told anything either.
            return False

        remaining = (deadline - utcnow()).total_seconds()
        if behaviour == GuestBehaviour.COOPERATIVE:
            self._spawn(self._simulate_exit(lease_id, max(0.05, remaining * 0.25)))
        elif behaviour == GuestBehaviour.SLOW:
            self._spawn(self._simulate_exit(lease_id, max(0.1, remaining * 0.88)))
        # IGNORES_NOTICE and TEARDOWN_STALLS schedule nothing: the reaper's
        # deadline is what ends those leases.
        return True

    async def _simulate_exit(self, lease_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        if lease_id in self._stopped:
            return
        self._stopped.add(lease_id)
        if self._on_clean_exit is not None:
            try:
                await self._on_clean_exit(lease_id)
            except Exception as exc:  # noqa: BLE001 - simulator must not crash the app
                log.warning("hypervisor.clean_exit_callback_failed",
                            lease_id=lease_id, error=str(exc))

    async def force_stop(
        self, *, lease_id: str, host_group: str, instance_ids: Sequence[str]
    ) -> None:
        """Stop the instances whether or not the guest agreed. Idempotent."""
        if self.behaviour_for(lease_id) == GuestBehaviour.HOST_UNREACHABLE:
            raise HostUnreachable(host_group, "force_stop")
        self._stopped.add(lease_id)
        edge(
            log, 24, f"force stopped {len(instance_ids)} instance(s)",
            lease_id=lease_id, host_group=host_group,
        )

    async def teardown(
        self, *, lease_id: str, instance_ids: Sequence[str], budget_seconds: float
    ) -> bool:
        """Detach volumes and release IPs. False means the budget was exceeded.

        A False here is what holds units in RECLAIMING. LLD §11: a stalled
        teardown must never look like free capacity.
        """
        behaviour = self.behaviour_for(lease_id)

        if behaviour == GuestBehaviour.TEARDOWN_STALLS_FOREVER:
            log.warning(
                "hypervisor.teardown_stalled",
                lease_id=lease_id,
                budget_seconds=budget_seconds,
                permanent=True,
                note="volumes/IPs never release; units stay RECLAIMING until a "
                "human intervenes — never falsely reported free",
            )
            return False

        if behaviour == GuestBehaviour.TEARDOWN_STALLS:
            attempt = self._teardown_attempts.get(lease_id, 0) + 1
            self._teardown_attempts[lease_id] = attempt
            # Two misses, then it releases. A real stall is usually a slow
            # volume detach that resolves, and the sweeper existing to recover
            # from it is only demonstrated if recovery is reachable.
            if attempt <= 2:
                log.warning(
                    "hypervisor.teardown_stalled",
                    lease_id=lease_id,
                    attempt=attempt,
                    budget_seconds=budget_seconds,
                    note="volumes/IPs not released yet; units stay RECLAIMING",
                )
                return False
            log.info(
                "hypervisor.teardown_recovered", lease_id=lease_id, attempts=attempt
            )
        # Real teardown is IO-bound and takes seconds; the simulator keeps the
        # ordering without the wall-clock cost.
        await asyncio.sleep(0)
        return True

    async def destroy(self, *, lease_id: str, instance_ids: Sequence[str]) -> None:
        """Last resort, and the escalation path when force_stop fails."""
        self._destroyed.add(lease_id)
        self._stopped.add(lease_id)
        self._instances.pop(lease_id, None)
        edge(
            log, 12, f"destroyed {len(instance_ids)} instance(s)",
            lease_id=lease_id,
        )

    # -- introspection for ops endpoints and tests -------------------------
    def is_stopped(self, lease_id: str) -> bool:
        return lease_id in self._stopped

    def is_destroyed(self, lease_id: str) -> bool:
        return lease_id in self._destroyed


class HttpHypervisor:
    """Live backend: libvirt / Nova / K8s behind an HTTP provisioning API."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._base = (settings.hypervisor_url or "").rstrip("/")
        self._caller = ExternalCaller("hypervisor", settings)

    def set_clean_exit_callback(
        self, callback: Callable[[str], Awaitable[None]]
    ) -> None:
        """No-op: in production the host agent calls the internal endpoint itself."""
        return None

    async def aclose(self) -> None:
        return None

    async def create(
        self, *, lease_id: str, host_group: str, flavour: str, count: int
    ) -> list[str]:
        async def send() -> list[str]:
            response = await self._client.post(
                f"{self._base}/instances",
                json={
                    "lease_id": lease_id,
                    "host_group": host_group,
                    "flavour": flavour,
                    "count": count,
                    "idempotency_key": lease_id,
                },
            )
            if response.status_code >= 500:
                raise ExternalError("hypervisor", "create", f"HTTP {response.status_code}")
            if response.status_code >= 400:
                raise ExternalError(
                    "hypervisor", "create", f"HTTP {response.status_code}",
                    retryable=False,
                )
            return [str(i) for i in response.json().get("instance_ids", [])]

        return await self._caller.call("create", send, timeout=15.0)

    async def deliver_notice(
        self, *, lease_id: str, instance_ids: Sequence[str], deadline: datetime
    ) -> bool:
        try:
            await self._caller.call(
                "deliver_notice",
                lambda: self._notice(lease_id, instance_ids, deadline),
                timeout=2.0,
            )
        except ExternalError as exc:
            log.warning("hypervisor.notice_failed", lease_id=lease_id, error=str(exc))
            return False
        return True

    async def _notice(
        self, lease_id: str, instance_ids: Sequence[str], deadline: datetime
    ) -> None:
        response = await self._client.post(
            f"{self._base}/instances/notice",
            json={
                "lease_id": lease_id,
                "instance_ids": list(instance_ids),
                "deadline": deadline.isoformat(),
            },
        )
        if response.status_code >= 400:
            raise ExternalError(
                "hypervisor", "deliver_notice", f"HTTP {response.status_code}",
                retryable=response.status_code >= 500,
            )

    async def force_stop(
        self, *, lease_id: str, host_group: str, instance_ids: Sequence[str]
    ) -> None:
        async def send() -> None:
            response = await self._client.post(
                f"{self._base}/instances/force-stop",
                json={"lease_id": lease_id, "instance_ids": list(instance_ids)},
            )
            if response.status_code in (502, 503, 504):
                raise HostUnreachable(host_group, "force_stop")
            if response.status_code >= 400:
                raise ExternalError(
                    "hypervisor", "force_stop", f"HTTP {response.status_code}",
                    retryable=response.status_code >= 500,
                )

        await self._caller.call("force_stop", send, timeout=10.0, retries=2)

    async def teardown(
        self, *, lease_id: str, instance_ids: Sequence[str], budget_seconds: float
    ) -> bool:
        async def send() -> bool:
            response = await self._client.post(
                f"{self._base}/instances/teardown",
                json={"lease_id": lease_id, "instance_ids": list(instance_ids)},
            )
            if response.status_code >= 500:
                raise ExternalError("hypervisor", "teardown", f"HTTP {response.status_code}")
            if response.status_code >= 400:
                return False
            body = response.json()
            # Both must be true: LLD §9 says teardown confirms volumes *and* IPs.
            return bool(body.get("volumes_released")) and bool(body.get("ips_released"))

        try:
            return await self._caller.call(
                "teardown", send, timeout=budget_seconds, retries=1,
                retry_on_timeout=False,
            )
        except ExternalError:
            return False

    async def destroy(self, *, lease_id: str, instance_ids: Sequence[str]) -> None:
        async def send() -> None:
            response = await self._client.delete(
                f"{self._base}/instances", params={"lease_id": lease_id}
            )
            if response.status_code >= 500:
                raise ExternalError("hypervisor", "destroy", f"HTTP {response.status_code}")

        await self._caller.call("destroy", send, timeout=15.0)
