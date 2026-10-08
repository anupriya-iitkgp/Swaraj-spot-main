"""Request-scoped dependencies: the container, the caller, and the rate limiter."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header, Request

from ..container import Container
from ..domain.errors import RateLimited, Unauthenticated
from ..logging import get_logger

log = get_logger(__name__)

__all__ = ["get_container", "TenantId", "IdempotencyKey", "ContainerDep", "enforce_rate_limit"]


def get_container(request: Request) -> Container:
    return request.app.state.container


ContainerDep = Annotated[Container, Depends(get_container)]


async def tenant_id(
    x_tenant_id: Annotated[str | None, Header(alias="X-Tenant-Id")] = None,
) -> str:
    """The authenticated tenant.

    In production this is populated by the gateway from a verified token; the
    header is the internal representation of that identity. It is required
    rather than defaulted — HLD §9's rejection table has no anonymous path, and
    a default tenant would be a way to spend someone else's quota.
    """
    if not x_tenant_id:
        raise Unauthenticated(
            "X-Tenant-Id is required; spot launches are never anonymous"
        )
    return x_tenant_id


TenantId = Annotated[str, Depends(tenant_id)]


async def idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str | None:
    return idempotency_key


IdempotencyKey = Annotated[str | None, Depends(idempotency_key)]


async def enforce_rate_limit(
    container: Container, tenant: str, *, cost: float = 1.0
) -> None:
    """Shared token bucket — LLD §12.9.

    HLD §12: "A pool that is briefly empty will reject many launches at once.
    Automation that retries immediately turns a capacity shortage into a
    self-inflicted API flood. Return Retry-After and enforce it at the gateway."

    The bucket is in Postgres rather than in each process, so the limit is per
    tenant across the whole deployment. A per-replica limiter silently divides
    the intended limit by the replica count and changes every time the
    deployment scales.
    """
    settings = container.settings
    if not settings.rate_limit_enabled:
        return

    allowed, wait = await container.rate_limit_repo.take(
        tenant,
        burst=settings.rate_limit_burst,
        refill_per_sec=settings.rate_limit_refill_per_sec,
        cost=cost,
    )
    if not allowed:
        raise RateLimited(
            "too many requests; honour the Retry-After header",
            details={
                "burst": settings.rate_limit_burst,
                "refill_per_second": settings.rate_limit_refill_per_sec,
            },
            retry_after=max(1, int(wait) + 1),
        )
