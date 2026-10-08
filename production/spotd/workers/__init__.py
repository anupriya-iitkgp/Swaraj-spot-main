"""Background loops.

Which loops elect a leader and which do not is a design decision, not a detail:

  leader-elected   pool_refresher, analytics
                   Duplicating them wastes work and makes the control-cycle
                   sequence meaningless, and neither is customer-facing.

  every replica    grace_reaper, outbox_relay, fulfilment_sweeper,
                   teardown_sweeper, retention_sweeper
                   These claim individual rows with FOR UPDATE SKIP LOCKED, so
                   running everywhere is safe and increases throughput. The
                   reaper especially must never wait for an election — it is the
                   last line of defence for a promise made to a customer.
"""

from .base import LeaderElectedWorker, PeriodicWorker
from .grace_reaper import GraceReaper
from .maintenance import (
    AnalyticsWorker,
    FulfilmentSweeper,
    PoolRefresher,
    RetentionSweeper,
    TeardownSweeper,
)
from .outbox_relay import OutboxRelay

__all__ = [
    "AnalyticsWorker",
    "FulfilmentSweeper",
    "GraceReaper",
    "LeaderElectedWorker",
    "OutboxRelay",
    "PeriodicWorker",
    "PoolRefresher",
    "RetentionSweeper",
    "TeardownSweeper",
]
