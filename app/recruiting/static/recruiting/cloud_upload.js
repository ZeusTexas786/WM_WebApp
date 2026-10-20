/* Same-origin, dependency-free sequential quarantine uploads. */
(() => {
  'use strict';
  const form = document.querySelector('#cloud-intake');
  const token = () => document.querySelector('[name=csrfmiddlewaretoken]').value;
  async function request(url, body, options = {}) {
    const response = await fetch(url, {
      method: body === undefined ? 'GET' : 'POST', credentials: 'same-origin',
      headers: { 'X-CSRFToken': token(), 'Content-Type': options.binary ? 'application/octet-stream' : 'application/json' },
      body: body === undefined ? undefined : (options.binary ? body : JSON.stringify(body)), signal: options.signal
    });
    const data = await response.json().catch(() => ({ error: 'The upload service is unavailable.' }));
    if (!response.ok) {
      const error = new Error(data.error || 'This file could not be uploaded.');
      error.retryable = response.status >= 500;
      throw error;
    }
    return data;
  }
  const sample = document.querySelector('#cloud-sample');
  if (sample) sample.addEventListener('submit', async event => {
    event.preventDefault();
    sample.querySelector('button').disabled = true;
    try { window.location.assign((await request('/batches/sample/', {})).url); }
    catch (error) { sample.querySelector('[role=alert]').textContent = error.message; sample.querySelector('button').disabled = false; }
  });
  if (!form) return;
  const uploadButton = document.querySelector('#cloud-upload');
  const submitButton = document.querySelector('#cloud-submit');
  const progress = document.querySelector('#cloud-progress');
  const errorBox = document.querySelector('#cloud-error');
  let batch = form.dataset.batch;
  let active = null;
  let generation = 0;
  let complete = false;
  const base = () => `/batches/${batch}/`;
  const inputBase = id => `${base()}uploads/${id}/`;
  const sleep = milliseconds => new Promise(resolve => window.setTimeout(resolve, milliseconds));
  async function digest(file) {
    if (!window.crypto?.subtle) throw new Error('A secure browser connection is required to verify files.');
    const bytes = await file.arrayBuffer();
    const hash = await window.crypto.subtle.digest('SHA-256', bytes);
    return Array.from(new Uint8Array(hash), value => value.toString(16).padStart(2, '0')).join('');
  }
  document.querySelector('#cloud-cancel').addEventListener('click', () => {
    if (active) {
      active.cancelled = true; generation++; complete = false;
      submitButton.disabled = true; active.controller?.abort();
    }
  });
  uploadButton.addEventListener('click', async () => {
    errorBox.textContent = ''; progress.replaceChildren(); complete = false;
    submitButton.disabled = true; uploadButton.disabled = true;
    try {
      const resumes = Array.from(form.elements.resumes.files);
      const jd = form.elements.jd_file?.files[0];
      const zip = resumes.some(file => file.name.toLowerCase().endsWith('.zip'));
      if (!resumes.length || resumes.length > 30 || (zip && resumes.length !== 1)) throw new Error('Choose up to 30 resumes or exactly one ZIP.');
      if (!batch && !form.elements.jd.value.trim() && !jd) throw new Error('Paste a job description or upload one JD.');
      const files = [...(jd ? [{ file: jd, role: 'jd' }] : []), ...resumes.map(file => ({ file, role: zip ? 'archive' : 'resume' }))];
      if (files.reduce((total, entry) => total + entry.file.size, 0) > 300*1024*1024) throw new Error('The uploaded batch exceeds 300 MB.');
      for (const entry of files) {
        const limit = entry.role === 'archive' ? 300*1024*1024 : 10*1024*1024;
        if (!entry.file.size || entry.file.size > limit) throw new Error(`${entry.file.name}: file is empty or exceeds its size limit.`);
        entry.line = document.createElement('li'); progress.append(entry.line);
        entry.line.textContent = `${entry.file.name}: verifying file…`;
        entry.hash = await digest(entry.file);
        entry.line.textContent = `${entry.file.name}: ready to upload`;
      }
      if (!batch) {
        const result = await request('/batches/new/', { title: form.elements.title.value, jd: form.elements.jd.value, input_mode: zip ? 'zip' : 'files' });
        batch = result.batch_id;
        window.history.replaceState({}, '', result.url);
      }
      const result = await request(`${base()}uploads/`, { files: files.map(entry => ({
        client_key: `${entry.role}:${entry.hash}`, filename: entry.file.name, role: entry.role, byte_size: entry.file.size, sha256: entry.hash
      })) });
      let cancelled = false;
      for (let index = 0; index < files.length; index++) {
        const entry = files[index]; const grant = result.uploads[index];
        const current = active = { id: grant.id, generation: ++generation, cancelled: false, controller: null };
        const assertCurrent = () => {
          if (current.cancelled || active !== current || current.generation !== generation) throw new DOMException('Cancelled', 'AbortError');
        };
        const currentRequest = async (url, body, options = {}) => {
          assertCurrent();
          current.controller = new AbortController();
          const reply = await request(url, body, { ...options, signal: current.controller.signal });
          assertCurrent();
          return reply;
        };
        let offset = grant.offset;
        try {
          while (offset < entry.file.size) {
            assertCurrent();
            const end = Math.min(offset + grant.chunk_bytes, entry.file.size);
            let sent = false;
            for (let attempt = 0; attempt < 3 && !sent; attempt++) {
              assertCurrent();
              try {
                const reply = await currentRequest(`${inputBase(grant.id)}chunk/?offset=${offset}`, entry.file.slice(offset, end), { binary: true });
                offset = reply.offset; sent = true;
              } catch (error) {
                assertCurrent();
                if (error.retryable === false || attempt === 2) throw error;
                await sleep(500 * (attempt + 1));
                assertCurrent();
                const committed = await currentRequest(inputBase(grant.id));
                offset = committed.offset;
                if (offset >= end) sent = true;
              }
            }
            entry.line.textContent = `${entry.file.name}: ${Math.round(offset / entry.file.size * 100)}% uploaded`;
          }
          await currentRequest(`${inputBase(grant.id)}finish/`, {});
          entry.line.textContent = `${entry.file.name}: received`;
        } catch (error) {
          if (current.cancelled || current.generation !== generation) {
            cancelled = true; complete = false; submitButton.disabled = true;
            entry.line.textContent = `${entry.file.name}: confirming cancellation...`;
            try {
              for (let attempt = 0; attempt < 3; attempt++) {
                try { await request(`${inputBase(grant.id)}cancel/`, {}); break; }
                catch (cancelError) {
                  if (cancelError.retryable === false || attempt === 2) throw cancelError;
                  await sleep(500 * (attempt + 1));
                }
              }
              entry.line.textContent = `${entry.file.name}: cancelled`;
            } catch (cancelError) {
              entry.line.textContent = `${entry.file.name}: cancellation not confirmed`;
              throw new Error(`${entry.file.name}: cancellation not confirmed. ${cancelError.message}`);
            }
          } else {
            entry.line.textContent = `${entry.file.name}: upload interrupted`;
            throw new Error(`${entry.file.name}: ${error.message} Reselect these files and click Upload files to resume.`);
          }
        }
        active = null;
      }
      complete = !cancelled;
      submitButton.disabled = !complete;
    } catch (error) { errorBox.textContent = error.message; }
    finally { active = null; uploadButton.disabled = false; }
  });
  submitButton.addEventListener('click', async () => {
    if (!complete) return;
    submitButton.disabled = true;
    try { window.location.assign((await request(`${base()}submit/`, {})).url); }
    catch (error) { errorBox.textContent = error.message; submitButton.disabled = false; }
  });
})();
