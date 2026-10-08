/**
 * The HTTP client.
 *
 * One rule shapes it: the API's typed rejections are *the product*, not
 * plumbing. HLD §7 makes a 409 "normal traffic on a busy pool, not an
 * incident", the error body carries `retry_after` and `alternatives`, and LLD
 * §7.1 gives every rejection a stable `code`. So errors are decoded into a
 * structured `ApiError` with those fields intact and surfaced to the operator
 * verbatim — a console that renders every failure as "something went wrong"
 * throws away the most useful thing this API produces.
 */

export class ApiError extends Error {
  constructor(status, payload, headers) {
    const error = (payload && payload.error) || {};
    super(error.message || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = error.code || 'http_error';
    this.details = error.details || {};
    this.retryAfter = Number(headers?.get?.('retry-after')) || this.details.retry_after || null;
    this.requestId = headers?.get?.('x-request-id') || null;
    this.payload = payload;
  }

  /** Every rejection in LLD §7.1 that a customer can act on, explained. */
  get guidance() {
    switch (this.code) {
      case 'no_capacity':
        return `The pool could not reserve those units. This is expected on a busy pool — retry after ${this.retryAfter ?? 5}s, or try an AZ from the alternatives.`;
      case 'quota_exceeded':
        return `The tenant is at its spot ceiling (${this.details.quota_units ?? '?'} units). Release a lease or raise the quota.`;
      case 'not_entitled':
        return 'The account service does not class this tenant as SPOT. Entitlement is re-read on every launch and never inferred from the request.';
      case 'flavour_not_spot_eligible':
        return 'That flavour is licence-bound and never sold as spot. The eligible list is in the error details.';
      case 'rate_limited':
        return 'The tenant is over its request budget. Honour Retry-After — LLD §12.9 exists because ignoring it turns a shortage into a flood.';
      case 'unauthenticated':
        return 'No identity on the request. Spot launches are never anonymous.';
      case 'out_of_scope_for_spot_subsystem':
        return 'That purchase option is served by the pay-per-use path, which is outside this subsystem (HLD §1).';
      case 'lease_not_found':
        return 'No lease with that id for this tenant.';
      default:
        return null;
    }
  }
}

async function request(method, path, { body, headers, signal } = {}) {
  const init = {
    method,
    headers: { accept: 'application/json', ...(headers || {}) },
    credentials: 'same-origin',
    signal,
  };
  if (body !== undefined) {
    init.headers['content-type'] = 'application/json';
    init.body = JSON.stringify(body);
  }

  let response;
  try {
    response = await fetch(path, init);
  } catch (cause) {
    if (cause.name === 'AbortError') throw cause;
    throw new ApiError(0, {
      error: { code: 'unreachable', message: 'the service did not answer' },
    });
  }

  if (response.status === 204) return null;

  const contentType = response.headers.get('content-type') || '';
  const payload = contentType.includes('json')
    ? await response.json().catch(() => null)
    : await response.text();

  if (!response.ok) throw new ApiError(response.status, payload, response.headers);
  return payload;
}

const query = (params) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value == null || value === '') continue;
    if (Array.isArray(value)) value.forEach((v) => search.append(key, v));
    else search.set(key, value);
  }
  const string = search.toString();
  return string ? `?${string}` : '';
};

/* ===================================================================== */
/* the customer contract — exactly what any tenant's own client would use */
/* ===================================================================== */
export const spot = {
  inventory: (az) => request('GET', `/spot/inventory${query({ az })}`),

  launch: (tenant, body, idempotencyKey) =>
    request('POST', '/spot/leases', {
      body,
      headers: {
        'X-Tenant-Id': tenant,
        ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}),
      },
    }),

  /** Through the gateway, so account classification and routing are exercised. */
  launchViaGateway: (tenant, body, idempotencyKey) =>
    request('POST', '/v1/instances', {
      body,
      headers: {
        'X-Tenant-Id': tenant,
        ...(idempotencyKey ? { 'Idempotency-Key': idempotencyKey } : {}),
      },
    }),

  leases: (tenant, params) =>
    request('GET', `/spot/leases${query(params)}`, { headers: { 'X-Tenant-Id': tenant } }),

  lease: (tenant, id) =>
    request('GET', `/spot/leases/${encodeURIComponent(id)}`, {
      headers: { 'X-Tenant-Id': tenant },
    }),

  release: (tenant, id) =>
    request('DELETE', `/spot/leases/${encodeURIComponent(id)}`, {
      headers: { 'X-Tenant-Id': tenant },
    }),

  interruptions: (params) => request('GET', `/spot/interruptions${query(params)}`),

  events: (tenant, params) =>
    request('GET', `/spot/events${query(params)}`, { headers: { 'X-Tenant-Id': tenant } }),
};

/* ===================================================================== */
/* the operator surface — session-gated, cross-tenant                    */
/* ===================================================================== */
export const console_ = {
  session: () => request('GET', '/console/session'),
  login: (token) => request('POST', '/console/session', { body: { token } }),
  logout: () => request('DELETE', '/console/session'),

  overview: (hours) => request('GET', `/console/overview${query({ hours })}`),
  timeline: (params) => request('GET', `/console/timeline${query(params)}`),
  leases: (params) => request('GET', `/console/leases${query(params)}`),
  reclaimOrders: (params) => request('GET', `/console/reclaim-orders${query(params)}`),
  reclaimOrder: (id) => request('GET', `/console/reclaim-orders/${encodeURIComponent(id)}`),
  audit: (params) => request('GET', `/console/audit${query(params)}`),
  events: (params) => request('GET', `/console/events${query(params)}`),
  tenants: () => request('GET', '/console/tenants'),
  hostGroups: (az) => request('GET', `/console/host-groups${query({ az })}`),
  invoice: (id) => request('GET', `/console/invoice/${encodeURIComponent(id)}`),

  // Actions. Each is session-authenticated here and signed server-side before
  // it reaches the handler — the browser never holds the key (LLD §12.1).
  reclaim: (body) => request('POST', '/console/actions/reclaim', { body }),
  headroom: (body) => request('POST', '/console/actions/headroom', { body }),
  controlCycle: () => request('POST', '/console/actions/control-cycle'),
  guestBehaviour: (leaseId, behaviour) =>
    request('POST', `/console/actions/guest-behaviour/${encodeURIComponent(leaseId)}${query({ behaviour })}`),
  cleanExit: (leaseId) =>
    request('POST', `/console/actions/clean-exit/${encodeURIComponent(leaseId)}`),
  quarantine: (hostGroup, reason) =>
    request('POST', `/console/actions/quarantine/${encodeURIComponent(hostGroup)}${query({ reason })}`),
  releaseQuarantine: (hostGroup) =>
    request('DELETE', `/console/actions/quarantine/${encodeURIComponent(hostGroup)}`),
  burst: (params) => request('POST', `/console/actions/burst${query(params)}`),
};

export const ops = {
  health: () => request('GET', '/health'),
  slo: (hours) => request('GET', `/ops/slo${query({ hours })}`),
  pools: () => request('GET', '/ops/pools'),
  fairness: (hours) => request('GET', `/ops/fairness${query({ hours })}`),
  noticeDelivery: (hours) => request('GET', `/ops/notice-delivery${query({ hours })}`),
  reconciliation: () => request('GET', '/ops/reconciliation'),
  config: () => request('GET', '/ops/config'),
  auditForLease: (id) => request('GET', `/ops/audit/${encodeURIComponent(id)}`),
  invoice: (id) => request('GET', `/ops/invoice/${encodeURIComponent(id)}`),
};

export { request };
