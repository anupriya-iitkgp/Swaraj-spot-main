/**
 * Boot-failure reporting. Classic script, loaded before the app module.
 *
 * A module that fails to parse, fails to fetch, or throws on its first line
 * never runs — so nothing inside the app can report it, and the boot spinner
 * turns forever. To someone looking at it, an unreachable console and a broken
 * one are the same picture. This file is deliberately not a module and imports
 * nothing, so it survives whatever broke the app.
 */

(function () {
  var reported = false;

  function fail(what, detail) {
    if (reported) return;
    reported = true;
    var boot = document.getElementById('boot');
    if (!boot) return;

    boot.textContent = '';

    var title = document.createElement('h1');
    title.textContent = 'The console failed to start';

    var lede = document.createElement('p');
    lede.textContent = what;

    var pre = document.createElement('pre');
    pre.style.cssText = 'max-width:min(900px,90vw);overflow-x:auto;text-align:left;'
      + 'font-size:12px;line-height:1.45;padding:12px;border-radius:8px;'
      + 'background:rgba(127,127,127,0.12);white-space:pre-wrap';
    pre.textContent = detail;

    var note = document.createElement('p');
    note.textContent = 'The API is unaffected — the contract is documented at /docs.';

    boot.appendChild(title);
    boot.appendChild(lede);
    boot.appendChild(pre);
    boot.appendChild(note);
  }

  // Capture phase: a failed <script src> fires a non-bubbling error on the element.
  addEventListener('error', function (event) {
    if (event.target && event.target.tagName === 'SCRIPT') {
      fail('A script could not be loaded.', String(event.target.src || 'unknown source'));
    } else if (event.error) {
      fail(
        'An error was thrown while starting up.',
        (event.error.stack || event.error.message || String(event.error))
        + '\n\nat ' + event.filename + ':' + event.lineno + ':' + event.colno,
      );
    } else if (event.message) {
      fail('An error was thrown while starting up.',
        event.message + '\n\nat ' + event.filename + ':' + event.lineno);
    }
  }, true);

  addEventListener('unhandledrejection', function (event) {
    var reason = event.reason || {};
    fail('A promise rejected during startup.',
      reason.stack || reason.message || String(reason));
  });
})();
