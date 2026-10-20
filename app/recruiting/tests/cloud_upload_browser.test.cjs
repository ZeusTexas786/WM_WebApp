/* Execute the production queue with a small DOM boundary and controlled network. */
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');

const deferred = () => {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
};
function element() {
  return { disabled: false, textContent: '', children: [], listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    append(child) { this.children.push(child); },
    replaceChildren() { this.children = []; },
    click() { return this.listeners.click?.(); }
  };
}
function queueFixture(scenario) {
  const calls = [];
  const waiting = deferred();
  const release = deferred();
  const content = new Blob(['Python resume']); content.name = 'resume.txt';
  const form = { dataset: { batch: 'batch-one' }, elements: { resumes: { files: [content] } } };
  const upload = element(), submit = element(), cancel = element(), progress = element(), errors = element();
  const nodes = { '#cloud-intake': form, '#cloud-upload': upload, '#cloud-submit': submit,
    '#cloud-cancel': cancel, '#cloud-progress': progress, '#cloud-error': errors,
    '[name=csrfmiddlewaretoken]': { value: 'csrf' } };
  const response = data => ({ ok: true, status: 200, json: async () => data });
  let chunkCalls = 0;
  let cancellationCalls = 0;
  const context = {
    Blob, Uint8Array, Array, AbortController, DOMException,
    document: { querySelector: selector => nodes[selector] || null, createElement: element },
    window: { crypto: webcrypto, history: { replaceState() {} }, location: { assign() {} },
      setTimeout(callback) {
        if (scenario === 'resume') callback();
        else { waiting.resolve(); release.promise.then(callback); }
      } },
    async fetch(url, options) {
      calls.push({ url, options });
      if (url.endsWith('/uploads/')) return response({ uploads: [{ id: 'input-one', offset: 0, chunk_bytes: 1024*1024 }] });
      if (url.includes('/chunk/')) {
        chunkCalls++;
        if (chunkCalls === 1 && !scenario.startsWith('finish') && scenario !== 'success') throw new TypeError('Lost chunk reply');
        return response({ offset: 13 });
      }
      if (url.endsWith('/finish/')) {
        if (scenario.startsWith('finish')) { waiting.resolve(); await release.promise; }
        return response({ complete: true });
      }
      if (url.endsWith('/cancel/')) {
        cancellationCalls++;
        if (scenario === 'finish-cancel-lost' && cancellationCalls === 1) throw new TypeError('Cancellation reply lost');
        return response({ cancelled: true });
      }
      if (url.endsWith('/input-one/')) {
        if (scenario === 'resume') { waiting.resolve(); await release.promise; }
        return response({ offset: 0 });
      }
      throw new Error('Unexpected request: ' + url);
    }
  };
  vm.runInNewContext(readFileSync(join(__dirname, '../static/recruiting/cloud_upload.js'), 'utf8'), context);
  return { calls, upload, submit, cancel, progress, errors, waiting, release };
}

test('cancel during retry backoff never sends another chunk or finish', async () => {
  const fixture = queueFixture('backoff');
  const running = fixture.upload.click();
  await fixture.waiting.promise;
  fixture.cancel.click();
  fixture.release.resolve();
  await running;
  assert.equal(fixture.calls.filter(call => call.url.includes('/chunk/')).length, 1);
  assert.equal(fixture.calls.filter(call => call.url.endsWith('/finish/')).length, 0);
  assert.equal(fixture.calls.filter(call => call.url.endsWith('/cancel/')).length, 1);
  assert.equal(fixture.submit.disabled, true);
  assert.match(fixture.progress.children[0].textContent, /cancelled/);
});

test('cancel during finish reconciles a late successful response and never enables Submit', async () => {
  const fixture = queueFixture('finish');
  const running = fixture.upload.click();
  await fixture.waiting.promise;
  fixture.cancel.click();
  fixture.release.resolve();
  await running;
  const finish = fixture.calls.find(call => call.url.endsWith('/finish/'));
  assert.equal(fixture.calls.filter(call => call.url.endsWith('/cancel/')).length, 1);
  assert.equal(finish.options.signal.aborted, true);
  assert.equal(fixture.submit.disabled, true);
  assert.match(fixture.progress.children[0].textContent, /cancelled/);
});

test('successful uploads still require and enable explicit Submit', async () => {
  const fixture = queueFixture('success');
  await fixture.upload.click();
  assert.equal(fixture.calls.filter(call => call.url.endsWith('/finish/')).length, 1);
  assert.equal(fixture.calls.filter(call => call.url.endsWith('/cancel/')).length, 0);
  assert.equal(fixture.submit.disabled, false);
  assert.match(fixture.progress.children[0].textContent, /received/);
});


test('cancel during offset reconciliation fences its late reply', async () => {
  const fixture = queueFixture('resume');
  const running = fixture.upload.click();
  await fixture.waiting.promise;
  fixture.cancel.click();
  fixture.release.resolve();
  await running;
  const resume = fixture.calls.find(call => call.url.endsWith('/input-one/'));
  assert.equal(resume.options.signal.aborted, true);
  assert.equal(fixture.calls.filter(call => call.url.includes('/chunk/')).length, 1);
  assert.equal(fixture.calls.filter(call => call.url.endsWith('/finish/')).length, 0);
  assert.equal(fixture.calls.filter(call => call.url.endsWith('/cancel/')).length, 1);
  assert.equal(fixture.submit.disabled, true);
  assert.match(fixture.progress.children[0].textContent, /cancelled/);
});


test('a lost cancellation reply is retried and never reports received', async () => {
  const fixture = queueFixture('finish-cancel-lost');
  const running = fixture.upload.click();
  await fixture.waiting.promise;
  fixture.cancel.click();
  fixture.release.resolve();
  await running;
  assert.equal(fixture.calls.filter(call => call.url.endsWith('/cancel/')).length, 2);
  assert.equal(fixture.submit.disabled, true);
  assert.match(fixture.progress.children[0].textContent, /cancelled/);
  assert.equal(fixture.errors.textContent, '');
});
