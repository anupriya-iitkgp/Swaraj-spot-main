"""Tenants, flavours and host groups.

These are the facts the Eligibility Guard checks against. HLD §6 is emphatic
that the Guard "must not trust a class supplied by the caller", so entitlement
is always read from here (or from the Account Service behind it) and never
taken from a header, a token claim or a request field.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

import asyncpg

from ...domain.models import AccountClass, Flavour, HostGroup, Tenant
from ...logging import get_logger

log = get_logger(__name__)

__all__ = ["ReferenceRepository"]


def _tenant(row: asyncpg.Record) -> Tenant:
    return Tenant(
        tenant_id=row["tenant_id"],
        name=row["name"],
        account_class=AccountClass(row["account_class"]),
        spot_quota_units=row["spot_quota_units"],
        concurrency_cap=row["concurrency_cap"],
        webhook_url=row["webhook_url"],
        contract_tier=row["contract_tier"],
        active=row["active"],
    )


def _flavour(row: asyncpg.Record) -> Flavour:
    return Flavour(
        name=row["name"],
        vcpu=row["vcpu"],
        memory_gb=row["memory_gb"],
        spot_eligible=row["spot_eligible"],
        licence_bound=row["licence_bound"],
        family=row["family"],
    )


def _host_group(row: asyncpg.Record) -> HostGroup:
    return HostGroup(
        host_group=row["host_group"],
        az=row["az"],
        total_units=row["total_units"],
        quarantined=row["quarantined"],
    )


class ReferenceRepository:
    def __init__(self, db: Any) -> None:
        self._db = db

    # -- tenants -----------------------------------------------------------
    async def get_tenant(
        self, tenant_id: str, *, conn: asyncpg.Connection | None = None
    ) -> Tenant | None:
        row = await (conn or self._db).fetchrow(
            """
            SELECT tenant_id, name, account_class, spot_quota_units,
                   concurrency_cap, webhook_url, contract_tier, active
              FROM tenant WHERE tenant_id = $1
            """,
            tenant_id,
        )
        return _tenant(row) if row else None

    async def upsert_tenant(
        self, tenant: Tenant, *, conn: asyncpg.Connection | None = None
    ) -> None:
        await (conn or self._db).execute(
            """
            INSERT INTO tenant (tenant_id, name, account_class, spot_quota_units,
                                concurrency_cap, webhook_url, contract_tier, active)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
            ON CONFLICT (tenant_id) DO UPDATE SET
                name = EXCLUDED.name,
                account_class = EXCLUDED.account_class,
                spot_quota_units = EXCLUDED.spot_quota_units,
                concurrency_cap = EXCLUDED.concurrency_cap,
                webhook_url = EXCLUDED.webhook_url,
                contract_tier = EXCLUDED.contract_tier,
                active = EXCLUDED.active
            """,
            tenant.tenant_id,
            tenant.name,
            tenant.account_class.value,
            tenant.spot_quota_units,
            tenant.concurrency_cap,
            tenant.webhook_url,
            tenant.contract_tier,
            tenant.active,
        )

    async def list_tenants(
        self,
        *,
        account_class: AccountClass | None = None,
        conn: asyncpg.Connection | None = None,
    ) -> list[Tenant]:
        if account_class:
            rows = await (conn or self._db).fetch(
                """
                SELECT tenant_id, name, account_class, spot_quota_units,
                       concurrency_cap, webhook_url, contract_tier, active
                  FROM tenant WHERE account_class = $1 ORDER BY tenant_id
                """,
                account_class.value,
            )
        else:
            rows = await (conn or self._db).fetch(
                """
                SELECT tenant_id, name, account_class, spot_quota_units,
                       concurrency_cap, webhook_url, contract_tier, active
                  FROM tenant ORDER BY tenant_id
                """
            )
        return [_tenant(r) for r in rows]

    # -- flavours ----------------------------------------------------------
    async def get_flavour(
        self, name: str, *, conn: asyncpg.Connection | None = None
    ) -> Flavour | None:
        row = await (conn or self._db).fetchrow(
            """
            SELECT name, vcpu, memory_gb, spot_eligible, licence_bound, family
              FROM flavour WHERE name = $1
            """,
            name,
        )
        return _flavour(row) if row else None

    async def list_flavours(
        self, *, spot_only: bool = False, conn: asyncpg.Connection | None = None
    ) -> list[Flavour]:
        clause = "WHERE spot_eligible" if spot_only else ""
        rows = await (conn or self._db).fetch(
            f"""
            SELECT name, vcpu, memory_gb, spot_eligible, licence_bound, family
              FROM flavour {clause} ORDER BY vcpu, name
            """
        )
        return [_flavour(r) for r in rows]

    async def upsert_flavour(
        self, flavour: Flavour, *, conn: asyncpg.Connection | None = None
    ) -> None:
        await (conn or self._db).execute(
            """
            INSERT INTO flavour (name, vcpu, memory_gb, spot_eligible,
                                 licence_bound, family)
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (name) DO UPDATE SET
                vcpu = EXCLUDED.vcpu,
                memory_gb = EXCLUDED.memory_gb,
                spot_eligible = EXCLUDED.spot_eligible,
                licence_bound = EXCLUDED.licence_bound,
                family = EXCLUDED.family
            """,
            flavour.name,
            flavour.vcpu,
            flavour.memory_gb,
            flavour.spot_eligible,
            flavour.licence_bound,
            flavour.family,
        )

    # -- host groups -------------------------------------------------------
    async def list_host_groups(
        self,
        *,
        az: str | None = None,
        include_quarantined: bool = False,
        conn: asyncpg.Connection | None = None,
    ) -> list[HostGroup]:
        clauses, args = [], []
        if az:
            args.append(az)
            clauses.append(f"az = ${len(args)}")
        if not include_quarantined:
            clauses.append("NOT quarantined")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = await (conn or self._db).fetch(
            f"""
            SELECT host_group, az, total_units, quarantined
              FROM host_group {where} ORDER BY host_group
            """,
            *args,
        )
        return [_host_group(r) for r in rows]

    async def upsert_host_group(
        self, group: HostGroup, *, conn: asyncpg.Connection | None = None
    ) -> None:
        await (conn or self._db).execute(
            """
            INSERT INTO host_group (host_group, az, total_units, quarantined)
            VALUES ($1,$2,$3,$4)
            ON CONFLICT (host_group) DO UPDATE SET
                az = EXCLUDED.az,
                total_units = EXCLUDED.total_units,
                quarantined = EXCLUDED.quarantined
            """,
            group.host_group,
            group.az,
            group.total_units,
            group.quarantined,
        )

    async def quarantine_host_group(
        self, host_group: str, reason: str, *, conn: asyncpg.Connection | None = None
    ) -> None:
        """Take a host out of the spot pool.

        LLD §11 pairs "host agent unreachable" with "escalate to hypervisor
        destroy" and "quarantine host from the spot pool". Without the
        quarantine, placement keeps choosing the broken host and every lease
        that lands there repeats the same failure.
        """
        await (conn or self._db).execute(
            """
            UPDATE host_group
               SET quarantined = true, quarantined_at = now(), quarantine_reason = $2
             WHERE host_group = $1
            """,
            host_group,
            reason,
        )
        log.error(
            "host_group.quarantined",
            host_group=host_group,
            reason=reason,
            remediation="host is excluded from placement until manually cleared",
        )

    async def release_quarantine(
        self, host_group: str, *, conn: asyncpg.Connection | None = None
    ) -> None:
        await (conn or self._db).execute(
            """
            UPDATE host_group
               SET quarantined = false, quarantined_at = NULL, quarantine_reason = NULL
             WHERE host_group = $1
            """,
            host_group,
        )
        log.info("host_group.quarantine_released", host_group=host_group)

    async def capacity_by_az(
        self, *, conn: asyncpg.Connection | None = None
    ) -> dict[str, int]:
        rows = await (conn or self._db).fetch(
            """
            SELECT az, SUM(total_units)::int AS units
              FROM host_group WHERE NOT quarantined GROUP BY az
            """
        )
        return {r["az"]: r["units"] for r in rows}
