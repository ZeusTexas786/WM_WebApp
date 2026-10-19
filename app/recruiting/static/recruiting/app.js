document.querySelectorAll('a.nav-link').forEach(link => {
  if (location.pathname === link.getAttribute('href')) link.setAttribute('aria-current', 'page');
});
document.querySelectorAll('[data-print]').forEach(button => button.addEventListener('click', () => window.print()));
document.querySelectorAll('[data-dropzone]').forEach(zone => {
  const input = zone.querySelector('input[type=file]');
  const list = zone.querySelector('.file-list');
  const update = () => {
    list.replaceChildren();
    Array.from(input.files).forEach(file => {
      const item = document.createElement('li');
      item.textContent = `${file.name} · ${(file.size / 1024).toFixed(0)} KB`;
      list.append(item);
    });
  };
  input.addEventListener('change', update);
  ['dragenter', 'dragover'].forEach(type => zone.addEventListener(type, event => {event.preventDefault(); zone.classList.add('dragover');}));
  ['dragleave', 'drop'].forEach(type => zone.addEventListener(type, event => {event.preventDefault(); zone.classList.remove('dragover');}));
  zone.addEventListener('drop', event => {input.files = event.dataTransfer.files; update();});
});
document.querySelectorAll('[data-busy-form]').forEach(form => form.addEventListener('submit', () => {
  const button = form.querySelector('button[type=submit],button:not([type])');
  if (button) {button.disabled = true; button.textContent = 'Working…';}
}));
const pending = document.querySelector('[data-poll-jobs], [data-interview-state=pending]');
if (pending) {
  let initial = null;
  let failures = 0;
  const poll = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch(pending.dataset.statusUrl, {headers: {'Accept': 'application/json'}});
      if (!response.ok) return;
      const data = await response.json();
      const snapshot = JSON.stringify(data);
      if (initial !== null && snapshot !== initial) {location.reload(); return;}
      initial = snapshot;
      if (!['queued','processing'].includes(data.rubric_status) && data.jobs.every(job => !['queued', 'processing'].includes(job.status) && !job.interview_pending)) {
        location.reload(); return;
      }
      failures = 0;
    } catch (_) {
      failures += 1;
      if (failures === 3) {
        const status = pending.querySelector('[role=status]');
        if (status) status.textContent = 'Connection interrupted. You can refresh to check progress.';
      }
    }
  };
  setInterval(poll, 2500);
  poll();
}
