/* Small, bounded status checks; no candidate content or provider calls. */
(() => {
  'use strict';
  const panel = document.querySelector('[data-status-url]');
  if (!panel) return;
  const url = panel.dataset.statusUrl;
  if (!/^\/batches\/[0-9a-f-]{36}\/status\/$/.test(url)) return;
  const initial = panel.dataset.stage;
  const deadline = Date.now() + 15 * 60 * 1000;
  let delay = 5000;
  const poll = async () => {
    if (Date.now() > deadline) return;
    if (document.hidden) { window.setTimeout(poll, 30000); return; }
    try {
      const response = await fetch(url, {credentials:'same-origin', cache:'no-store'});
      if (!response.ok || response.redirected) throw new Error('Status unavailable');
      const status = await response.json();
      if (typeof status.stage !== 'string' || typeof status.terminal !== 'boolean') throw new Error('Invalid status');
      if (status.terminal || status.stage !== initial) { window.location.reload(); return; }
      delay = 5000;
    } catch (_) { delay = Math.min(delay * 2, 30000); }
    window.setTimeout(poll, delay);
  };
  window.setTimeout(poll, delay);
})();
