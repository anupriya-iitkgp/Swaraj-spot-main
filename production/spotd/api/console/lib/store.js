/**
 * State, polling and routing.
 *
 * Two behaviours here are deliberate rather than incidental:
 *
 * **No skeleton on refetch.** A poller that clears its data before the next
 * response arrives makes a 1 Hz dashboard flash once a second, and a flashing
 * dashboard is one nobody watches during an incident. The previous render is
 * held, dimmed, until fresh data lands.
 *
 * **Polling stops when the tab is hidden.** A background tab hammering
 * `/console/overview` is load the service did not need to carry, and — because
 * the console shares the API's own metrics — load that shows up in the numbers
 * the operator is trying to read.
 */

const listeners = new Set();

export const state = {
  route: { path: '/', params: {} },
  mode: 'tenant',           // 'tenant' | 'ops'
  session: null,            // console session descriptor
  tenantId: localStorage.getItem('spot.tenant') || '',
  theme: localStorage.getItem('spot.theme') || 'auto',
  toasts: [],
  drawer: null,
};

let toastSeq = 0;

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function notify() {
  for (const fn of listeners) fn();
}

export function set(patch) {
  Object.assign(state, patch);
  notify();
}

export function setTenant(tenantId) {
  state.tenantId = tenantId;
  localStorage.setItem('spot.tenant', tenantId);
  notify();
}

export function setTheme(theme) {
  state.theme = theme;
  localStorage.setItem('spot.theme', theme);
  applyTheme();
  notify();
}

export function applyTheme() {
  const root = document.documentElement;
  if (state.theme === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', state.theme);
}

/* --------------------------------------------------------------------- */
/* toasts                                                                 */
/* --------------------------------------------------------------------- */
export function toast(kind, title, body, { ttl = 6000 } = {}) {
  const id = ++toastSeq;
  state.toasts = [...state.toasts, { id, kind, title, body }];
  notify();
  if (ttl) setTimeout(() => dismissToast(id), ttl);
  return id;
}

export function dismissToast(id) {
  state.toasts = state.toasts.filter((t) => t.id !== id);
  notify();
}

/** Report an ApiError with everything it carries. */
export function toastError(error, context) {
  if (error?.name === 'AbortError') return;
  const detail = [error?.guidance, error?.requestId && `request ${error.requestId}`]
    .filter(Boolean)
    .join(' · ');
  toast('error', context || error?.code || 'Request failed', `${error?.message || error}${detail ? ` — ${detail}` : ''}`, { ttl: 10000 });
}

/* --------------------------------------------------------------------- */
/* routing — hash-free, history API, with the server fallback in static.py */
/* --------------------------------------------------------------------- */
export function navigate(path, { replace = false } = {}) {
  if (path === location.pathname + location.search) return;
  if (replace) history.replaceState({}, '', path);
  else history.pushState({}, '', path);
  readRoute();
}

export function readRoute() {
  const url = new URL(location.href);
  state.route = {
    path: url.pathname.replace(/\/+$/, '') || '/',
    params: Object.fromEntries(url.searchParams),
  };
  state.mode = state.route.path.startsWith('/operator') ? 'ops' : 'tenant';
  notify();
}

export function link(path) {
  return {
    href: path,
    onClick: (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
      event.preventDefault();
      navigate(path);
    },
  };
}

window.addEventListener('popstate', readRoute);

/* --------------------------------------------------------------------- */
/* resource — a poll with a held previous value                           */
/* --------------------------------------------------------------------- */
export function resource(loader, { interval = 0, immediate = true } = {}) {
  const self = {
    data: null,
    error: null,
    loading: false,
    stale: false,
    loadedAt: null,
    _timer: null,
    _abort: null,
    _stopped: false,
  };

  self.refresh = async () => {
    if (self._abort) self._abort.abort();
    const controller = new AbortController();
    self._abort = controller;
    self.loading = true;
    self.stale = self.data != null;   // hold the previous render, dimmed
    notify();
    try {
      const data = await loader(controller.signal);
      if (controller.signal.aborted) return;
      self.data = data;
      self.error = null;
      self.loadedAt = Date.now();
    } catch (error) {
      if (error.name === 'AbortError') return;
      self.error = error;
    } finally {
      if (!controller.signal.aborted) {
        self.loading = false;
        self.stale = false;
        notify();
      }
    }
  };

  self.start = () => {
    if (self._stopped) return self;
    if (immediate) self.refresh();
    if (interval) {
      self._timer = setInterval(() => {
        if (document.visibilityState === 'visible') self.refresh();
      }, interval);
    }
    return self;
  };

  self.stop = () => {
    self._stopped = true;
    if (self._timer) clearInterval(self._timer);
    if (self._abort) self._abort.abort();
  };

  return self;
}

/**
 * A shared 1 Hz tick, for anything that counts down between polls.
 *
 * Countdowns are computed from `force_stop_deadline` — the timestamp the reaper
 * itself acts on — rather than decremented locally, so a tab that was throttled
 * or asleep shows the real remaining time instead of a drifted one.
 */
let tickHandle = null;
export function startTicker() {
  if (tickHandle) return;
  tickHandle = setInterval(() => {
    if (document.visibilityState === 'visible') notify();
  }, 1000);
}
