"""The customer-facing surface, plus the gateway hop.

HLD §6 lists the Spot Market API's operations exactly:

    GET /spot/inventory, POST /spot/leases, GET|DELETE /spot/leases/{id}

`POST /v1/instances` is the gateway (edges 1-3). It looks up the account class,
routes SPOT into this subsystem, and answers 501 for everything else — because
HLD §1 puts STATIC and DYNAMIC requests outside this design entirely, and
pretending to serve them would be worse than saying so.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request, Response, status

from ..db.repositories import fingerprint
from ..domain.errors import NoCapacity, OutOfScope, RateLimited, UnknownTenant
from ..domain.models import AccountClass, PurchaseOption
from ..logging import edge, get_logger
from .deps import ContainerDep, IdempotencyKey, TenantId, enforce_rate_limit
from .schemas import (
    InventoryEntryResponse,
    InventoryResponse,
    LaunchRequest,
    LaunchResponse,
    LeaseResponse,
    lease_to_response,
)

log = get_logger(__name__)

router = APIRouter(tags=["spot"])


# ======================================================================
# GET /spot/inventory — edge 6
# ======================================================================
@router.get("/spot/inventory", response_model=InventoryResponse)
async def inventory(
    container: ContainerDep,
    az: Annotated[str | None, Query(description="filter to one AZ")] = None,
) -> InventoryResponse:
    entries = await container.market.inventory(az=az)
    return InventoryResponse(
        pools=[
            InventoryEntryResponse(
                az=e.az,
                flavour=e.flavour,
                vcpu=e.vcpu,
                memory_gb=e.memory_gb,
                available_instances=e.available_instances,
                available_units=e.available_units,
                discount=e.discount,
                price_per_hour=e.price_per_hour,
                list_price_per_hour=e.list_price_per_hour,
                interruption_rate_per_lease_hour=e.interruption_rate,
                interruption_sample_hours=e.interruption_sample_hours,
                pool_staleness_seconds=e.pool_staleness_seconds,
                degraded=e.degraded,
            )
            for e in entries
        ],
        control_cycle_seconds=container.settings.control_cycle,
    )


# ======================================================================
# POST /spot/leases — edges 3, 5, 7, 8, 4
# ======================================================================
@router.post(
    "/spot/leases",
    response_model=LaunchResponse,
    status_code=status.HTTP_201_CREATED,
)
async def launch(
    body: LaunchRequest,
    container: ContainerDep,
    tenant: TenantId,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> LaunchResponse:
    await enforce_rate_limit(container, tenant)

    # A launch without an idempotency key still gets one. HLD §11 requires a
    # retry after a network partition not to double-allocate, and a client that
    # omits the header is exactly the client most likely to retry blindly.
    key = idempotency_key or f"auto-{uuid.uuid4().hex}"

    try:
        result = await container.market.launch(
            tenant_id=tenant,
            flavour=body.flavour,
            count=body.count,
            az=body.az,
            idempotency_key=key,
            request_fingerprint=fingerprint(body.fingerprint_fields()),
            purchase_option=body.purchase_option,
        )
    except NoCapacity as exc:
        # Charge the retry-storm penalty *before* the response goes out, so a
        # client that ignores Retry-After hits the limiter on its next call
        # rather than several calls later (LLD §12.9).
        if container.settings.rate_limit_enabled:
            await container.rate_limit_repo.penalise(
                tenant, container.settings.rate_limit_penalty_on_409
            )
        raise

    if not result.replayed:
        # Edges 9-12 run off the request path (HLD §5 marks them async), so the
        # 201 does not wait for placement or boot.
        container.spawn_fulfilment(result.lease.lease_id)
        response.status_code = status.HTTP_201_CREATED
        message = (
            "admitted; capacity is reserved and provisioning has started. "
            "Poll GET /spot/leases/{id} or subscribe to the event stream."
        )
    else:
        response.status_code = status.HTTP_200_OK
        message = "idempotent replay: this is the lease your original request created"

    response.headers["Location"] = f"/spot/leases/{result.lease.lease_id}"
    return LaunchResponse(
        lease=lease_to_response(result.lease),
        idempotent_replay=result.replayed,
        message=message,
    )


# ======================================================================
# GET /spot/leases, GET|DELETE /spot/leases/{id} — edge 32
# ======================================================================
@router.get("/spot/leases", response_model=list[LeaseResponse])
async def list_leases(
    container: ContainerDep,
    tenant: TenantId,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[LeaseResponse]:
    leases = await container.market.list_leases(tenant, limit=limit, offset=offset)
    return [lease_to_response(lease) for lease in leases]


@router.get("/spot/leases/{lease_id}", response_model=LeaseResponse)
async def describe(
    lease_id: str, container: ContainerDep, tenant: TenantId
) -> LeaseResponse:
    return lease_to_response(await container.market.describe(lease_id, tenant))


@router.delete("/spot/leases/{lease_id}", response_model=LeaseResponse)
async def terminate(
    lease_id: str, container: ContainerDep, tenant: TenantId
) -> LeaseResponse:
    await enforce_rate_limit(container, tenant)
    return lease_to_response(await container.market.terminate(lease_id, tenant))


# ======================================================================
# GET /spot/interruptions — edge 30
# ======================================================================
@router.get("/spot/interruptions")
async def interruptions(
    container: ContainerDep,
    flavour: str | None = None,
    az: str | None = None,
) -> dict[str, Any]:
    return await container.market.interruption_feed(flavour=flavour, az=az)


# ======================================================================
# GET /spot/events — the tenant event stream (edge 17, third channel)
# ======================================================================
@router.get("/spot/events")
async def events(
    container: ContainerDep,
    tenant: TenantId,
    since_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Events for this tenant, published from the outbox.

    HLD §12 recommends "a capacity-watch subscription so tenants wait on an
    event instead of polling" as the answer to retry storms. This is the read
    side of that; a long-poll or SSE transport is a transport change, not a
    model change.
    """
    matched = [
        e
        for e in container.bus.recent(since_seq=since_seq, limit=limit * 4)
        if e.payload.get("tenant_id") == tenant
    ][-limit:]
    return {
        "events": [e.as_dict() for e in matched],
        "latest_seq": container.bus._seq,  # noqa: SLF001 - read-only cursor
    }


# ======================================================================
# POST /v1/instances — the gateway (edges 1, 2, 3)
# ======================================================================
@router.post("/v1/instances", tags=["gateway"])
async def gateway_launch(
    body: LaunchRequest,
    container: ContainerDep,
    tenant: TenantId,
    response: Response,
    idempotency_key: IdempotencyKey = None,
) -> Any:
    """Classify, then route.

    HLD §2: the account class answers only "is this tenant allowed to use spot?"
    It says nothing about whether the request can be served — that is the
    Admission Controller's decision, against a pool whose size changes every
    control cycle.

    Note the class is looked up here for *routing* and then re-derived by the
    Eligibility Guard for *authorisation*. That duplication is deliberate: HLD
    §6 forbids the Guard from trusting a class supplied by its caller, and this
    handler is a caller like any other.
    """
    account_class = await container.externals.accounts.get_account_class(tenant)
    if account_class is None:
        raise UnknownTenant(
            "the account service does not recognise this tenant; spot access is "
            "refused rather than assumed"
        )

    edge(log, 1, f"account class {account_class.value}", tenant_id=tenant)

    effective = body.purchase_option or (
        PurchaseOption.SPOT
        if account_class is AccountClass.SPOT
        else PurchaseOption.ON_DEMAND
    )
    if effective is not PurchaseOption.SPOT:
        raise OutOfScope(
            f"purchase option {effective.value} is handled by the pay-per-use "
            f"path, which is out of scope for this service (HLD §1)",
            details={
                "account_class": account_class.value,
                "purchase_option": effective.value,
                "in_scope": "spot",
            },
        )

    edge(log, 3, "routing to Spot Market API", tenant_id=tenant)
    return await launch(
        body=body,
        container=container,
        tenant=tenant,
        response=response,
        idempotency_key=idempotency_key,
    )
