/**
 * Console entry point: theme, session, routing, chrome.
 *
 * A view is a factory returning `{ render, dispose }`. The factory runs once
 * per route, starts whatever pollers it needs, and is disposed on navigation —
 * so leaving a page stops its requests rather than leaving a poller running in
 * the background against an endpoint nobody is looking at.
 */

import { h, render } from './lib/dom.js';
import { icons } from './lib/ui.js';
import * as api from './lib/api.js';
import {
  state, subscribe, notify, set, readRoute, link, navigate,
  applyTheme, setTheme, startTicker, dismissToast, toastError, toast,
} from './lib/store.js';

import createMarket from './views/tenant/market.js';
import createTenantLeases from './views/tenant/leases.js';
import createLeaseDetail from './views/tenant/lease.js';
import createTenantActivity from './views/tenant/activity.js';

import createOpsOverview from './views/ops/overview.js';
import createOpsPools from './views/ops/pools.js';
import createOpsFleet from './views/ops/fleet.js';
import createOpsReclaim from './views/ops/reclaim.js';
import createOpsSlo from './views/ops/slo.js';
import createOpsAudit from './views/ops/audit.js';
import createOpsTenants from './views/ops/tenants.js';
import createOpsConfig from './views/ops/config.js';
import createLogin from './views/login.js';

/* --------------------------------------------------------------------- */
/* routes                                                                 */
/* --------------------------------------------------------------------- */
const ROUTES = [
  { path: '/',            factory: createMarket,         mode: 'tenant' },
  { path: '/leases',      factory: createTenantLeases,   mode: 'tenant' },
  { match: /^\/leases\/(.+)$/, factory: createLeaseDetail, mode: 'tenant' },
  { path: '/activity',    factory: createTenantActivity, mode: 'tenant' },

  { path: '/operator',         factory: createOpsOverview, mode: 'ops', auth: true },
  { path: '/operator/pools',   factory: createOpsPools,    mode: 'ops', auth: true },
  { path: '/operator/fleet',   factory: createOpsFleet,    mode: 'ops', auth: true },
  { path: '/operator/reclaim', factory: createOpsReclaim,  mode: 'ops', auth: true },
  { path: '/operator/slo',     factory: createOpsSlo,      mode: 'ops', auth: true },
  { path: '/operator/audit',   factory: createOpsAudit,    mode: 'ops', auth: true },
  { path: '/operator/tenants', factory: createOpsTenants,  mode: 'ops', auth: true },
  { path: '/operator/config',  factory: createOpsConfig,   mode: 'ops', auth: true },
];

const TENANT_NAV = [
  { path: '/',         label: 'Market',   icon: icons.cart },
  { path: '/leases',   label: 'Leases',   icon: icons.server },
  { path: '/activity', label: 'Activity', icon: icons.activity },
];

const OPS_NAV = [
  { path: '/operator',         label: 'Overview',  icon: icons.gauge },
  { path: '/operator/pools',   label: 'Pools',     icon: icons.layers },
  { path: '/operator/fleet',   label: 'Fleet',     icon: icons.server },
  { path: '/operator/reclaim', label: 'Reclaim',   icon: icons.bolt },
  { path: '/operator/slo',     label: 'SLO',       icon: icons.shield },
  { path: '/operator/audit',   label: 'Audit',     icon: icons.clipboard },
  { path: '/operator/tenants', label: 'Tenants',   icon: icons.users },
  { path: '/operator/config',  label: 'Config',    icon: icons.sliders },
];

function resolve(path) {
  for (const route of ROUTES) {
    if (route.path === path) return { route, args: [] };
    if (route.match) {
      const found = path.match(route.match);
      if (found) return { route, args: found.slice(1) };
    }
  }
  return null;
}

/* --------------------------------------------------------------------- */
/* view lifecycle                                                         */
/* --------------------------------------------------------------------- */
let current = null;          // { key, view, route }

function ensureView() {
  const resolved = resolve(state.route.path);
  if (!resolved) {
    disposeCurrent();
    current = { key: '404', view: notFound(), route: null };
    return;
  }

  const { route, args } = resolved;

  // Three states, not two. Until `GET /console/session` answers we do not know
  // whether this operator is signed in, and guessing either way is wrong:
  // guessing "signed in" starts a page whose every request 401s, and guessing
  // "signed out" flashes a login form at someone who already has a session.
  if (route.auth && state.session === null) {
    if (current?.key === 'checking') return;
    disposeCurrent();
    current = { key: 'checking', view: checkingSession(), route };
    return;
  }

  // An operator route without a session becomes the login screen rather than a
  // wall of 401s; the login view navigates back once the session exists.
  const needsLogin = route.auth && !state.session.authenticated;
  const key = needsLogin ? 'login' : `${route.path || route.match}|${args.join('|')}`;
  if (current && current.key === key) return;

  disposeCurrent();
  const factory = needsLogin ? createLogin : route.factory;
  current = { key, view: factory({ args, navigate }), route };
}

function checkingSession() {
  return {
    render: () => h('.page', h('.card', h('.empty', 'Checking your operator session…'))),
  };
}

function disposeCurrent() {
  if (current?.view?.dispose) current.view.dispose();
  current = null;
}

function notFound() {
  return {
    render: () => h('.page',
      h('.card',
        h('h1', 'No such page'),
        h('p.lede', 'That path is not part of the console.'),
        h('p', { style: { 'margin-top': '12px' } },
          h('a', link('/'), 'Back to the market')),
      ),
    ),
  };
}

/* --------------------------------------------------------------------- */
/* chrome                                                                 */
/* --------------------------------------------------------------------- */
function brandMark() {
  // Three bars: sellable, reserved, cooldown — the pool, at 22px.
  return h('svg.brand-mark', { viewBox: '0 0 22 22', 'aria-hidden': 'true' },
    h('rect', { x: 1, y: 12.5, width: 5, height: 8.5, rx: 1.5, fill: 'var(--series-1)' }),
    h('rect', { x: 8.5, y: 6, width: 5, height: 15, rx: 1.5, fill: 'var(--series-2)' }),
    h('rect', { x: 16, y: 1, width: 5, height: 20, rx: 1.5, fill: 'var(--series-3)' }),
  );
}

function topbar() {
  const session = state.session;
  const nextTheme = state.theme === 'dark' ? 'light' : 'dark';

  return h('header.topbar',
    h('.brand', brandMark(), 'Spot', h('small', 'ESDS')),

    h('nav.mode-switch', { 'aria-label': 'Console' },
      h('a', { ...link('/'), 'aria-current': String(state.mode === 'tenant') }, 'Tenant'),
      h('a', { ...link('/operator'), 'aria-current': String(state.mode === 'ops') }, 'Operator'),
    ),

    h('.topbar-spacer'),

    state.mode === 'tenant' && tenantPicker(),

    h('button.btn.ghost.small', {
      onClick: () => setTheme(nextTheme),
      title: `Switch to ${nextTheme} theme`,
      'aria-label': `Switch to ${nextTheme} theme`,
    }, state.theme === 'dark' ? icons.sun({ size: 15 }) : icons.moon({ size: 15 })),

    session?.authenticated && h('button.btn.ghost.small', {
      onClick: async () => {
        try {
          await api.console_.logout();
          set({ session: { authenticated: false, requires_token: session.requires_token } });
          toast('info', 'Signed out', 'The operator session has been cleared.');
          navigate('/');
        } catch (error) { toastError(error, 'Sign out failed'); }
      },
      title: 'End the operator session',
    }, icons.logout({ size: 15 }), 'Sign out'),
  );
}

function tenantPicker() {
  return h('.field', { style: { 'flex-direction': 'row', 'align-items': 'center', gap: '8px' } },
    h('label', { for: 'tenant-input', style: { 'white-space': 'nowrap' } }, 'Tenant'),
    h('input#tenant-input.input', {
      value: state.tenantId,
      placeholder: 'tenant-spot-01',
      style: { width: '190px', 'min-height': '30px', padding: '4px 9px' },
      title: 'The gateway derives this from a verified token in production; '
           + 'here it is typed, so any tenant can be exercised.',
      onInput: (event) => {
        state.tenantId = event.target.value.trim();
        localStorage.setItem('spot.tenant', state.tenantId);
      },
      onChange: () => notify(),
    }),
  );
}

function sidebar() {
  const items = state.mode === 'ops' ? OPS_NAV : TENANT_NAV;
  const groupLabel = state.mode === 'ops' ? 'Operations' : 'Spot market';

  return h('nav.sidebar', { 'aria-label': 'Sections' },
    h('.nav-group', groupLabel),
    items.map((item) => h('a.nav-link', {
      key: item.path,
      ...link(item.path),
      'aria-current': state.route.path === item.path ? 'page' : null,
    }, item.icon({ size: 15 }), item.label)),

    state.mode === 'ops' && state.session?.authenticated && h('div', { style: { 'margin-top': 'auto', 'padding-top': '20px' } },
      h('.nav-group', 'Session'),
      h('div', { style: { padding: '0 10px', 'font-size': '12px', color: 'var(--text-muted)' } },
        state.session.requires_token
          ? `Signed in · expires in ${Math.round((state.session.expires_in_seconds || 0) / 60)}m`
          : 'Open dev session — no token configured'),
    ),
  );
}

function toasts() {
  if (!state.toasts.length) return null;
  return h('.toasts', { role: 'status', 'aria-live': 'polite' },
    state.toasts.map((item) => h(`.toast.${item.kind}`, {
      key: item.id,
      onClick: () => dismissToast(item.id),
      style: { cursor: 'pointer' },
    },
      h('.toast-title', item.title),
      item.body && h('.toast-body', item.body),
    )),
  );
}

/* --------------------------------------------------------------------- */
/* render                                                                 */
/* --------------------------------------------------------------------- */
const root = document.getElementById('root');
let frame = null;
let painted = false;

/**
 * Coalesce renders into one per frame — but never the first one.
 *
 * State can change several times inside one turn (route read, session
 * resolved, a poller returning), and rendering each of those separately is
 * wasted work. A frame is the right granularity for that. It is the wrong
 * granularity for the *first* paint: deferring it leaves the boot spinner on
 * screen for an extra frame, and anything that captures the page at load —
 * a screenshot, a synthetic check, a slow machine mid-navigation — captures
 * the spinner instead of the app.
 */
function scheduleRender() {
  if (!painted) {
    painted = true;
    paint();
    return;
  }
  if (frame) return;
  frame = requestAnimationFrame(() => {
    frame = null;
    paint();
  });
}

function paint() {
  ensureView();
  try {
    render(root, [
      h('.app',
        topbar(),
        sidebar(),
        h('main.main', current?.view?.render() ?? null),
      ),
      toasts(),
    ]);
  } catch (error) {
    console.error('render failed', error);
    render(root, h('.page', h('.card',
      h('h1', 'The console hit an error'),
      h('p.lede', String(error?.message || error)),
      h('p', { style: { 'margin-top': '12px' } },
        h('button.btn', { onClick: () => location.reload() }, 'Reload')),
    )));
  }
}

/* --------------------------------------------------------------------- */
/* boot                                                                   */
/* --------------------------------------------------------------------- */
async function boot() {
  applyTheme();
  readRoute();
  subscribe(scheduleRender);
  startTicker();
  scheduleRender();

  try {
    set({ session: await api.console_.session() });
  } catch (error) {
    // A console that is switched off is a valid deployment (§12.1's other
    // half); the tenant side still works, so this is not an error to shout about.
    set({ session: { authenticated: false, requires_token: true, console_enabled: false } });
  }
}

boot();
