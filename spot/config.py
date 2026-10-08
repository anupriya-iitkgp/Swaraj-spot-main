"""Runtime configuration.

Defaults are DEMO-FAST so you can watch a full launch + reclaim cycle in seconds.
Production values are given in the comments and can be set via environment variables.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _s(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Config:
    # --- grace window (diagram: Grace Timer & Escalation, edges 23/24) -------
    # Production: grace_seconds=120, force_stop_at=95, teardown_budget=18
    grace_seconds: float = _f("SPOT_GRACE_SECONDS", 8.0)
    force_stop_at: float = _f("SPOT_FORCE_STOP_AT", 6.3)
    teardown_budget: float = _f("SPOT_TEARDOWN_BUDGET", 1.2)

    # --- control loop (edge 19: Forecast & Headroom -> Spot Pool View) ------
    # Production: 30-60 s
    control_cycle_seconds: float = _f("SPOT_CONTROL_CYCLE", 2.0)

    # --- admission ----------------------------------------------------------
    idempotency_ttl_seconds: float = _f("SPOT_IDEMPOTENCY_TTL", 86400.0)
    retry_after_seconds: int = _i("SPOT_RETRY_AFTER", 5)
    default_tenant_quota_units: int = _i("SPOT_TENANT_QUOTA", 64)

    # --- reclaim policy -----------------------------------------------------
    # Fraction of a single tenant's spot fleet that one reclaim wave may take.
    blast_radius_fraction: float = _f("SPOT_BLAST_RADIUS", 0.5)
    # Anti-thrash: reclaimed capacity may not be re-sold for this long.
    cooldown_seconds: float = _f("SPOT_COOLDOWN", 3.0)

    # --- pricing ------------------------------------------------------------
    # Spot discount is derived from surplus depth, clamped to this band.
    min_discount: float = _f("SPOT_MIN_DISCOUNT", 0.40)
    max_discount: float = _f("SPOT_MAX_DISCOUNT", 0.80)

    # --- demo knobs ---------------------------------------------------------
    # a manually raised forecast headroom reverts on its own, so a forgotten
    # demo click can never leave the pool advertising zero forever
    headroom_ttl_seconds: float = _f("SPOT_HEADROOM_TTL", 120.0)

    # --- telemetry ----------------------------------------------------------
    sample_interval_seconds: float = _f("SPOT_SAMPLE_INTERVAL", 2.0)

    # --- simulation knobs (external stubs only) -----------------------------
    hypervisor_create_seconds: float = _f("SIM_CREATE_SECONDS", 0.4)
    hypervisor_stop_seconds: float = _f("SIM_STOP_SECONDS", 0.3)
    teardown_seconds: float = _f("SIM_TEARDOWN_SECONDS", 0.5)

    # --- backend: "sim" (in-memory stubs) or "proxmox" (real hypervisor) ----
    # proxmox mode creates a real LXC container per instance and reads the
    # node's true capacity; if the API is unreachable at startup the app
    # falls back to sim and says so in /ops/overview.
    backend: str = _s("SPOT_BACKEND", "sim")
    proxmox_host: str = _s("SPOT_PROXMOX_HOST", "")            # e.g. 10.14.21.16
    proxmox_node: str = _s("SPOT_PROXMOX_NODE", "")            # e.g. stsbr02s02
    proxmox_token_id: str = _s("SPOT_PROXMOX_TOKEN_ID", "")    # user@realm!name
    proxmox_token_secret: str = _s("SPOT_PROXMOX_TOKEN_SECRET", "")
    proxmox_template: str = _s("SPOT_PROXMOX_TEMPLATE", "")    # (lxc, unused)
    proxmox_vm_template: int = _i("SPOT_PROXMOX_VM_TEMPLATE", 9000)  # qemu template vmid
    proxmox_storage: str = _s("SPOT_PROXMOX_STORAGE", "local-lvm")


CONFIG = Config()
