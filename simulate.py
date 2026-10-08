#!/usr/bin/env python3
"""Continuous traffic generator for the ops dashboard.

Drives the running HTTP API with a randomised workload so the dashboard
shows a live, moving system instead of a static one:

  - tenants launch spot leases of random flavours in random AZs
  - most guests drain politely on notice; some ignore it and get force-stopped
  - leases are released after a while (capacity returns, billing accrues)
  - forecast headroom rises and falls, triggering proactive reclaims
  - occasional direct reclaim orders arrive from the "capacity side"

Usage:
    uvicorn spot.api.app:app --port 8001        # terminal 1
    python simulate.py --port 8001              # terminal 2

Stop with Ctrl+C. Tune --pace to speed the whole thing up or down.
"""
from __future__ import annotations

import argparse
import asyncio
import random

import httpx

TENANTS = ["tenant-spot-a", "tenant-spot-b", "tenant-spot-c"]
FLAVOURS = ["s1.small", "s1.small", "s1.medium", "s1.medium", "s1.large"]
AZS = ["az-1", "az-1", "az-2"]  # az-1 twice: it is the bigger zone


class Simulator:
    def __init__(self, base_url: str, pace: float):
        self.client = httpx.AsyncClient(base_url=base_url, timeout=10.0)
        self.pace = pace  # multiplier on every sleep; lower = frenzied
        self.my_leases: list[str] = []  # leases this script launched

    async def sleep(self, lo: float, hi: float) -> None:
        await asyncio.sleep(random.uniform(lo, hi) * self.pace)

    # ── the actors ─────────────────────────────────────────────────────────

    async def launcher(self) -> None:
        """Tenants keep arriving with launch requests."""
        while True:
            tenant = random.choice(TENANTS)
            body = {
                "flavour": random.choice(FLAVOURS),
                "count": random.choice([1, 1, 1, 2]),
                "az": random.choice(AZS),
                # ~1 in 4 guests ignores the notice -> grace timer force-stops it
                "drain_seconds": None if random.random() < 0.25 else round(random.uniform(0.5, 3.0), 1),
            }
            r = await self.client.post("/spot/leases", json=body,
                                       headers={"X-Tenant-Id": tenant})
            if r.status_code == 201:
                lease = r.json()
                self.my_leases.append(lease["lease_id"])
                print(f"launch  {lease['lease_id']}  {tenant} {body['flavour']}x{body['count']} {body['az']}")
            else:
                print(f"launch rejected {r.status_code}: {r.json().get('detail', {})}")
            await self.sleep(2, 6)

    async def releaser(self) -> None:
        """Leases do not live forever: release old ones so billing closes."""
        while True:
            await self.sleep(6, 12)
            if len(self.my_leases) > 3:  # keep a few running
                lease_id = self.my_leases.pop(random.randrange(len(self.my_leases)))
                r = await self.client.delete(f"/spot/leases/{lease_id}")
                print(f"release {lease_id} -> {r.status_code}")

    async def forecaster(self) -> None:
        """Headroom drifts; a spike triggers the proactive reclaim path."""
        while True:
            await self.sleep(15, 30)
            az = random.choice(["az-1", "az-2"])
            if random.random() < 0.4:  # spike: force a shortfall -> reclaim
                units = random.randint(60, 95)
            else:  # calm: plenty of room again
                units = random.randint(0, 20)
            r = await self.client.post("/sim/headroom", json={"az": az, "units": units})
            out = r.json()
            tag = " -> RECLAIM" if out.get("reclaim") else ""
            print(f"headroom {az} = {units} (shortfall {out.get('shortfall_units')}){tag}")

    async def capacity_side(self) -> None:
        """Occasionally the capacity side just demands units back directly."""
        while True:
            await self.sleep(25, 45)
            body = {"units": random.choice([2, 4, 6]), "az": random.choice(AZS),
                    "reason": "simulated demand surge"}
            r = await self.client.post("/internal/spot/reclaim", json=body)
            print(f"reclaim order {body['units']}u {body['az']} -> {r.status_code}")

    async def run(self) -> None:
        r = await self.client.get("/healthz")
        r.raise_for_status()
        print(f"connected to {self.client.base_url} — Ctrl+C to stop\n")
        await asyncio.gather(self.launcher(), self.launcher(),  # two arrival streams
                             self.releaser(), self.forecaster(), self.capacity_side())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8001)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--pace", type=float, default=1.0,
                    help="multiplier on all delays: 0.5 = twice as busy, 2.0 = calmer")
    args = ap.parse_args()
    sim = Simulator(f"http://{args.host}:{args.port}", args.pace)
    try:
        asyncio.run(sim.run())
    except KeyboardInterrupt:
        print("\nsimulation stopped")


if __name__ == "__main__":
    main()
