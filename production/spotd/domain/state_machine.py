"""The lease state machine.

HLD §10 states it in one line:

    REQUESTED -> ADMITTED -> PROVISIONING -> RUNNING -> NOTICE_ISSUED
             -> DRAINING -> STOPPED -> CLOSED, plus REJECTED.

Two things it does not say, which are decided here and enforced everywhere:

1.  **Where an in-flight cancellation lands.** HLD §12 requires that a reclaim
    arriving during ADMITTED or PROVISIONING "cancels outright, no notice, no
    charge", and calls it a first-class path rather than an exception. It needs
    a destination state. Rather than invent a tenth state that the HLD does not
    list, ADMITTED and PROVISIONING transition straight to CLOSED with
    `preemption_reason = cancelled_in_flight`. "No charge" then falls out of the
    data instead of being a special case in the rating code: `running_at` is
    NULL, so there is no billable window to rate.

2.  **Which states are preemptible.** Only RUNNING. `preempt()` on anything else
    is a no-op, which is what makes two concurrent reclaim orders selecting the
    same lease safe (LLD §10.4).

The transition table is the single source of truth. Both the Python guard and
the Postgres CHECK constraint are generated from it, so they cannot drift.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final, Iterable

__all__ = [
    "LeaseState",
    "TRANSITIONS",
    "TERMINAL",
    "PREEMPTIBLE",
    "CANCELLABLE",
    "BILLABLE",
    "ACTIVE",
    "HOLDS_RESERVATION",
    "can_transition",
    "assert_transition",
    "IllegalTransition",
]


class LeaseState(StrEnum):
    REQUESTED = "REQUESTED"
    ADMITTED = "ADMITTED"
    PROVISIONING = "PROVISIONING"
    RUNNING = "RUNNING"
    NOTICE_ISSUED = "NOTICE_ISSUED"
    DRAINING = "DRAINING"
    STOPPED = "STOPPED"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


S = LeaseState

#: Legal transitions. Anything absent here is a bug, not an edge case.
TRANSITIONS: Final[dict[LeaseState, frozenset[LeaseState]]] = {
    S.REQUESTED: frozenset({S.ADMITTED, S.REJECTED}),
    # CLOSED from ADMITTED/PROVISIONING is the cancel-outright path (note 1).
    S.ADMITTED: frozenset({S.PROVISIONING, S.REJECTED, S.CLOSED}),
    S.PROVISIONING: frozenset({S.RUNNING, S.REJECTED, S.CLOSED}),
    # DRAINING direct from RUNNING is a customer-initiated release: the tenant
    # asked, so there is no notice to issue.
    S.RUNNING: frozenset({S.NOTICE_ISSUED, S.DRAINING}),
    # STOPPED direct from NOTICE_ISSUED is a guest that exited immediately.
    S.NOTICE_ISSUED: frozenset({S.DRAINING, S.STOPPED}),
    S.DRAINING: frozenset({S.STOPPED}),
    S.STOPPED: frozenset({S.CLOSED}),
    S.CLOSED: frozenset(),
    S.REJECTED: frozenset(),
}

TERMINAL: Final[frozenset[LeaseState]] = frozenset({S.CLOSED, S.REJECTED})

#: Only a RUNNING lease can be preempted. A lease that never ran is cancelled,
#: not preempted, and a lease already draining is being torn down anyway.
PREEMPTIBLE: Final[frozenset[LeaseState]] = frozenset({S.RUNNING})

#: Reclaim landing on one of these cancels outright: no notice, no charge.
CANCELLABLE: Final[frozenset[LeaseState]] = frozenset({S.ADMITTED, S.PROVISIONING})

#: States in which the meter is running. Note NOTICE_ISSUED and DRAINING are
#: absent: HLD §10 requires the grace window to be excluded from the invoice.
BILLABLE: Final[frozenset[LeaseState]] = frozenset({S.RUNNING})

#: States in which a lease is live from the customer's point of view.
ACTIVE: Final[frozenset[LeaseState]] = frozenset(
    {S.REQUESTED, S.ADMITTED, S.PROVISIONING, S.RUNNING, S.NOTICE_ISSUED, S.DRAINING}
)

#: States in which a lease still holds units in the pool.
#:
#: STOPPED belongs here and ACTIVE does not, which is the whole point of having
#: two sets. A stopped lease has released nothing: its reservation is given back
#: only when `finish_teardown` proves the capacity actually returned, so between
#: STOPPED and CLOSED the units are legitimately still held. Reconciling against
#: ACTIVE would report every in-flight teardown as pool drift, and an alert that
#: fires during normal operation is an alert nobody reads.
HOLDS_RESERVATION: Final[frozenset[LeaseState]] = ACTIVE | {S.STOPPED}


class IllegalTransition(RuntimeError):
    """A transition that the state machine forbids was attempted."""

    def __init__(self, current: LeaseState, target: LeaseState, lease_id: str) -> None:
        allowed = ", ".join(sorted(TRANSITIONS[current])) or "(none — terminal)"
        super().__init__(
            f"lease {lease_id}: cannot move {current} -> {target}; "
            f"legal targets are {allowed}"
        )
        self.current = current
        self.target = target
        self.lease_id = lease_id


def can_transition(current: LeaseState, target: LeaseState) -> bool:
    return target in TRANSITIONS[current]


def assert_transition(current: LeaseState, target: LeaseState, lease_id: str) -> None:
    if not can_transition(current, target):
        raise IllegalTransition(current, target, lease_id)


def reachable_from(state: LeaseState) -> frozenset[LeaseState]:
    """Transitive closure — used by tests to prove every state can reach a terminal."""
    seen: set[LeaseState] = set()
    frontier: list[LeaseState] = [state]
    while frontier:
        node = frontier.pop()
        for nxt in TRANSITIONS[node]:
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return frozenset(seen)


def sql_check_constraint(column: str = "state") -> str:
    """Render the state enum as a Postgres CHECK so the DB rejects junk too."""
    values = ", ".join(f"'{s.value}'" for s in LeaseState)
    return f"{column} IN ({values})"


def sql_transition_pairs() -> Iterable[tuple[str, str]]:
    """(from, to) pairs, for the migration that materialises the table."""
    for src, targets in TRANSITIONS.items():
        for dst in sorted(targets):
            yield src.value, dst.value
