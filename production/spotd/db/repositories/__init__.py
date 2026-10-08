"""Repositories — the only modules that issue SQL.

Split by aggregate, not by table, so a component depends on the thing it owns
rather than on the schema. `LeaseRepository` is the only writer of `spot_lease`,
which is how HLD §6's "single writer" rule survives the move from a dict to a
database.
"""

from .analytics import AnalyticsRepository, FairnessSpread
from .audit import AuditEvent, AuditRepository, ChainVerification
from .billing import BillingRepository, LedgerRepository
from .coordination import (
    LeaderRepository,
    LeaderState,
    NonceRepository,
    RateLimitRepository,
)
from .idempotency import IdempotencyRecord, IdempotencyRepository, fingerprint
from .leases import LeaseRepository, TransitionRejected, VictimCandidate
from .outbox import OutboxMessage, OutboxRepository, Topics
from .pool import PoolRepository, ReconcileResult, ReserveOutcome
from .reclaim import ReclaimRepository
from .reference import ReferenceRepository

__all__ = [
    "AnalyticsRepository",
    "AuditEvent",
    "AuditRepository",
    "BillingRepository",
    "ChainVerification",
    "FairnessSpread",
    "IdempotencyRecord",
    "IdempotencyRepository",
    "LeaderRepository",
    "LeaderState",
    "LeaseRepository",
    "LedgerRepository",
    "NonceRepository",
    "OutboxMessage",
    "OutboxRepository",
    "PoolRepository",
    "RateLimitRepository",
    "ReclaimRepository",
    "ReconcileResult",
    "ReferenceRepository",
    "ReserveOutcome",
    "Topics",
    "TransitionRejected",
    "VictimCandidate",
    "fingerprint",
]
