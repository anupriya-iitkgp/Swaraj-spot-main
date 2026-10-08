/**
 * Operator sign-in.
 *
 * The console can fire a reclaim order, and a reclaim order ends customer
 * workloads. That is why there is a login screen in front of a dashboard: the
 * signing key stays in the process, and this exchanges an operator credential
 * for a session that authorises the server to use it.
 */

import { h } from '../lib/dom.js';
import * as api from '../lib/api.js';
import { icons, banner } from '../lib/ui.js';
import { set, state, notify, toast, toastError, navigate } from '../lib/store.js';

export default function createLogin() {
  let token = '';
  let busy = false;

  async function submit(event) {
    event.preventDefault();
    busy = true;
    notify();
    try {
      const session = await api.console_.login(token);
      set({ session });
      toast('ok', 'Signed in', 'Privileged actions are signed server-side from here on.');
      navigate(state.route.path.startsWith('/operator') ? state.route.path : '/operator');
    } catch (error) {
      toastError(error, 'Sign-in refused');
    } finally {
      busy = false;
      notify();
    }
  }

  return {
    render: () => h('.login',
      h('.login-card',
        h('div',
          h('h1', 'Operator console'),
          h('p', { style: { color: 'var(--text-secondary)', 'margin-top': '6px' } },
            'Pools, the reclaim path, the SLO and the audit trail.'),
        ),

        state.session && !state.session.requires_token && banner('warning',
          h('strong', 'No operator token is configured. '),
          'Any credential opens a session on this deployment. Production configuration '
          + 'refuses to start this way.'),

        h('form', { onSubmit: submit, style: { display: 'flex', 'flex-direction': 'column', gap: '14px' } },
          h('.field',
            h('label', { for: 'token' }, 'Operator token'),
            h('input#token.input', {
              type: 'password',
              autocomplete: 'current-password',
              value: token,
              placeholder: state.session?.requires_token ? 'SPOT_CONSOLE_TOKEN' : 'anything (dev mode)',
              onInput: (event) => { token = event.target.value; },
            }),
            h('.hint', 'Exchanged for a session cookie. The HMAC key that authorises reclaim '
              + 'orders never leaves the server.'),
          ),
          h('button.btn.primary', { type: 'submit', disabled: busy },
            icons.shield({ size: 15 }), busy ? 'Signing in…' : 'Sign in'),
        ),
      ),

      h('p', { style: { color: 'var(--text-muted)', 'font-size': '13px' } },
        'No credential? ',
        h('a', {
          href: '/',
          onClick: (event) => { event.preventDefault(); navigate('/'); },
        }, 'The tenant console'),
        ' needs none — it only uses the published customer API.'),
    ),
  };
}
