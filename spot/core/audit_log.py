"""Preemption Audit Log (edges 27, 28, 29).

Immutable record of every notice, timer expiry, forced stop and credit — the
evidence base for disputes and for tuning victim selection. Append-only.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

log = logging.getLogger("spot.audit")


class PreemptionAuditLog:
    def __init__(self):
        self._entries: list[dict] = []

    def append(self, kind: str, **fields) -> dict:
        entry = {"seq": len(self._entries) + 1, "ts": time.time(), "kind": kind, **fields}
        self._entries.append(entry)
        return entry

    # convenience wrappers, one per audited fact ---------------------------
    def lease_transition(self, lease_id: str, src: str, dst: str, reason: str = "") -> None:
        self.append("lease_transition", lease_id=lease_id, src=src, dst=dst, reason=reason)

    def notice_issued(self, lease_id: str, order_id: str, grace_seconds: float) -> None:
        self.append("notice_issued", lease_id=lease_id, order_id=order_id,
                    grace_seconds=grace_seconds)

    def notice_delivery(self, lease_id: str, channels: list[str], ok: bool) -> None:
        self.append("notice_delivery", lease_id=lease_id, channels=channels, delivered=ok)

    def timer_expired(self, lease_id: str) -> None:
        self.append("timer_expired", lease_id=lease_id)

    def forced_stop(self, lease_id: str) -> None:
        self.append("forced_stop", lease_id=lease_id)

    def capacity_returned(self, lease_id: str, host_group: str, units: int) -> None:
        self.append("capacity_returned", lease_id=lease_id, host_group=host_group, units=units)

    def credit(self, lease_id: str, amount: float, reason: str) -> None:
        self.append("credit", lease_id=lease_id, amount=amount, reason=reason)

    def entries(self, lease_id: Optional[str] = None, kind: Optional[str] = None) -> list[dict]:
        out = self._entries
        if lease_id:
            out = [e for e in out if e.get("lease_id") == lease_id]
        if kind:
            out = [e for e in out if e["kind"] == kind]
        return list(out)

    def preemption_history(self) -> list[dict]:
        """Edge 29 — feeds Interruption Analytics."""
        return [e for e in self._entries if e["kind"] in {"notice_issued", "forced_stop"}]
