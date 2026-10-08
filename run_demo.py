#!/usr/bin/env python3
"""End-to-end walkthrough of the HLD, no HTTP server required.

    python run_demo.py

Traces the numbered edges from the wiring diagram as they fire:

  Act 1  a spot launch                    edges 1 → 2 → 3 → 5 → 6 → 7 → 8 → 9-12 → 4
  Act 2  rejections that must be cheap    edges 5, 7
  Act 3  a forecast-driven reclaim        edges 18 → 20 → 21 → 22 → 23 → 16/17 → 13 → 14 → 15
  Act 4  a guest that ignores the notice  edge 24 (forced stop)
  Act 5  reclaim arriving mid-provision   cancel outright: no notice, no charge
  Act 6  the money and the evidence       edges 25 → 26, 27/28 → 29 → 30
"""
from __future__ import annotations

import asyncio
import logging
import sys

from spot.config import CONFIG
from spot.container import Container
from spot.domain.errors import SpotError
from spot.domain.models import LeaseState

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
CYAN, GREEN, RED, YELLOW = "\033[36m", "\033[32m", "\033[31m", "\033[33m"


def act(n: int, title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─' * 78}{RESET}")
    print(f"{BOLD}{CYAN} ACT {n}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 78}{RESET}")


def say(msg: str, colour: str = "") -> None:
    print(f"  {colour}{msg}{RESET}")


async def wait_for(pred, timeout: float = 15.0, interval: float = 0.05) -> bool:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if pred():
            return True
        await asyncio.sleep(interval)
    return False


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format=f"{DIM}%(asctime)s %(name)-18s %(message)s{RESET}",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("spot.bus").setLevel(logging.WARNING)

    c = Container()
    await c.start()
    lm, market = c.lease_manager, c.market

    # a tenant webhook, so notice channel 2 is real
    received: list[dict] = []

    async def webhook(payload: dict) -> None:
        received.append(payload)

    c.notice_delivery.register_webhook("tenant-spot-a", webhook)

    try:
        # ------------------------------------------------------------------
        act(1, "A spot launch")
        say(f"pool before: {c.pool.snapshot()}", DIM)
        lease, _ = await market.launch(
            tenant_id="tenant-spot-a", flavour_name="s1.medium", count=2,
            az="az-1", idempotency_key="demo-key-1",
        )
        say(f"admitted {lease.lease_id}  units={lease.units} "
            f"discount={lease.discount_snapshot:.0%} rate={lease.rate_per_sec*3600:.3f}/h", GREEN)
        await wait_for(lambda: lease.state is LeaseState.RUNNING)
        say(f"state={lease.state.value} host_group={lease.host_group} "
            f"instances={lease.instance_ids}", GREEN)

        say("idempotent retry with the same key:", DIM)
        again, replayed = await market.launch(
            tenant_id="tenant-spot-a", flavour_name="s1.medium", count=2,
            az="az-1", idempotency_key="demo-key-1",
        )
        assert replayed and again.lease_id == lease.lease_id
        say(f"returned the ORIGINAL lease {again.lease_id} (replayed={replayed}) "
            "— no double allocation", GREEN)

        # ------------------------------------------------------------------
        act(2, "Rejections that have to be cheap")
        for tenant, flavour, count, note in [
            ("tenant-dynamic", "s1.small", 1, "wrong account class"),
            ("tenant-spot-a", "db1.xlarge", 1, "flavour not spot-eligible"),
            ("tenant-spot-a", "s1.large", 40, "quota / no capacity"),
        ]:
            try:
                await market.launch(tenant_id=tenant, flavour_name=flavour, count=count,
                                    az="az-1", idempotency_key=None)
                say(f"{note}: unexpectedly ACCEPTED", RED)
            except SpotError as exc:
                say(f"{note}: {exc.status_code} {exc.code} — {exc.message}", YELLOW)

        # ------------------------------------------------------------------
        act(3, "Forecast-driven reclaim (the proactive path)")
        other, _ = await market.launch(
            tenant_id="tenant-spot-b", flavour_name="s1.small", count=1,
            az="az-1", idempotency_key=None,
        )
        await wait_for(lambda: other.state is LeaseState.RUNNING)
        say(f"second lease {other.lease_id} running on {other.host_group}", DIM)

        say("letting the leases run for a moment so there is something to bill...", DIM)
        await asyncio.sleep(1.5)

        say("capacity side raises P95 headroom above free capacity -> shortfall", DIM)
        free_now = c.ledger.free_units("az-1")
        c.forecast.set_headroom("az-1", free_now + 6)   # 6 units must come back
        await c.pool.refresh()
        sold = sum(l.units for l in lm.live_leases(az="az-1"))
        shortfall = c.forecast.protected_shortfall("az-1", sold)
        say(f"live spot={sold} units, shortfall={shortfall} units", DIM)

        result = await c.reclaim_handler.handle(units=shortfall, az="az-1",
                                                reason="forecast headroom rise")
        say(f"edge 18/20/21 → order {result['order_id']} preempting "
            f"{len(result['preempted'])} lease(s)", YELLOW)
        say(f"tenant webhook received {len(received)} notice(s) (channel 2)", DIM)

        victims = [lm.get(i) for i in result["preempted"]]
        await wait_for(lambda: all(v.is_terminal for v in victims), timeout=CONFIG.grace_seconds + 6)
        for v in victims:
            slo = (v.closed_at - v.notice_at) if (v.closed_at and v.notice_at) else None
            say(f"{v.lease_id}: {v.state.value} forced={v.forced_stop} "
                f"notice→closed={slo:.2f}s (budget {CONFIG.grace_seconds}s) "
                f"channels={v.notice_channels_delivered}",
                GREEN if slo and slo <= CONFIG.grace_seconds else RED)
        say(f"ledger after reclaim: {c.ledger.snapshot()}", DIM)

        # ------------------------------------------------------------------
        act(4, "A guest that ignores the notice — the timer wins")
        c.forecast.set_headroom("az-1", 4)
        await c.pool.refresh()
        stubborn, _ = await market.launch(
            tenant_id="tenant-spot-c", flavour_name="s1.small", count=1, az="az-1",
            idempotency_key=None, drain_seconds=None,   # never exits on its own
        )
        await wait_for(lambda: stubborn.state is LeaseState.RUNNING)
        r = await c.reclaim_handler.handle(units=stubborn.units, az="az-1",
                                           host_group=stubborn.host_group, reason="demo")
        assert stubborn.lease_id in r["preempted"]
        await wait_for(lambda: stubborn.is_terminal, timeout=CONFIG.grace_seconds + 6)
        say(f"{stubborn.lease_id}: {stubborn.state.value} forced_stop={stubborn.forced_stop} "
            f"(force fired at T+{CONFIG.force_stop_at}s of a {CONFIG.grace_seconds}s window)",
            GREEN if stubborn.forced_stop else RED)

        # ------------------------------------------------------------------
        act(5, "Reclaim landing while the lease is still PROVISIONING")
        c.forecast.set_headroom("az-2", 0)
        await c.pool.refresh()
        inflight, _ = await market.launch(
            tenant_id="tenant-spot-a", flavour_name="s1.small", count=1,
            az="az-2", idempotency_key=None,
        )
        say(f"{inflight.lease_id} is {inflight.state.value}; reclaiming immediately", DIM)
        await c.reclaim_handler.handle(units=inflight.units, az="az-2", reason="demo mid-flight")
        await wait_for(lambda: inflight.is_terminal)
        notices = c.audit.entries(lease_id=inflight.lease_id, kind="notice_issued")
        say(f"{inflight.lease_id}: {inflight.state.value}, notices issued={len(notices)}, "
            f"billed={inflight.amount:.6f} — cancelled outright, no notice, no charge",
            GREEN if not notices and inflight.amount == 0 else RED)

        # ------------------------------------------------------------------
        act(6, "The money and the evidence")
        await market.drain_background()
        say("releasing every surviving lease so the invoices are complete", DIM)
        for l in list(lm.all()):
            if not l.is_terminal:
                await lm.release(l.lease_id)
        await wait_for(lambda: all(l.is_terminal for l in lm.all()), timeout=8)
        for tenant in ("tenant-spot-a", "tenant-spot-b", "tenant-spot-c"):
            inv = c.billing.invoice_for(tenant)
            say(f"{tenant}: {len(inv['charges'])} charge(s), "
                f"{len(inv['credits'])} credit(s), total {inv['total']:.6f}")
        say("")
        say("interruption rate published back into the inventory feed (edge 30):", DIM)
        for row in c.analytics.rate_by_flavour_az():
            say(f"  {row['flavour']:<12} {row['az']}  {row['interrupted']}/{row['leases']} "
                f"= {row['interruption_rate']:.0%}")
        say("")
        say("audit log (edges 27/28) — last 12 entries:", DIM)
        for e in c.audit.entries()[-12:]:
            extra = {k: v for k, v in e.items() if k not in ("seq", "ts", "kind")}
            say(f"  #{e['seq']:<3} {e['kind']:<20} {extra}")

        print(f"\n{BOLD}{GREEN}Demo complete.{RESET} "
              f"Start the HTTP API with: uvicorn spot.api.app:app --reload\n")
        return 0
    finally:
        await c.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
