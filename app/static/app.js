/* ═══════════════════════════════════════════════════════════════════════
   Musicdrome UI.

   Vanilla, no build step, no framework. Every view is a render function that
   takes fresh JSON from the API and rewrites its container; there is no
   client-side state to drift out of sync with the server.

   Two polls keep things live: a slow one for scan state, and a fast one that
   only runs while a download is actually in flight.
   ═══════════════════════════════════════════════════════════════════════ */

'use strict';

const state = {
  tab: 'discover',
  status: 'new',
  minMatch: 0,
  sort: 'match',
  tag: '',
  downloadStatus: 'all',
  days: 90,
  settings: {},
  scanning: false,
  fastPoll: null,
  statusPoll: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

// ─── API ────────────────────────────────────────────────────────────────

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
  return body;
}

function toast(message, isError = false) {
  const element = $('#toast');
  element.textContent = message;
  element.classList.toggle('bad', isError);
  element.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.hidden = true; }, 4000);
}

// ─── Formatting ─────────────────────────────────────────────────────────

const escapeHtml = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (char) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));

function tier(match) {
  if (match >= 85) return 'tier-high';
  if (match >= 70) return 'tier-mid';
  if (match >= 50) return 'tier-low';
  return 'tier-weak';
}

function bytes(value) {
  if (!value) return '—';
  const units = ['B', 'KB', 'MB', 'GB'];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
  return `${size.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function ago(timestamp) {
  if (!timestamp) return 'never';
  const seconds = Math.floor(Date.now() / 1000) - timestamp;
  if (seconds < 60) return 'just now';
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

const initials = (artist) =>
  artist.split(/\s+/).slice(0, 2).map((word) => word[0] || '').join('').toUpperCase();

// ─── Discover ───────────────────────────────────────────────────────────

async function loadDiscover() {
  const query = new URLSearchParams({
    status: state.status,
    min_match: state.minMatch,
    sort: state.sort,
  });
  if (state.tag) query.set('tag', state.tag);

  const data = await api(`/suggestions?${query}`);
  renderTags(data.tags);
  renderCards(data.suggestions);
}

function renderTags(tags) {
  const container = $('#f-tags');
  container.innerHTML = tags
    .map((tag) => `
      <button class="chip ${state.tag === tag.name ? 'is-on' : ''}" data-tag="${escapeHtml(tag.name)}">
        ${escapeHtml(tag.name)}<span class="count">${tag.count}</span>
      </button>`)
    .join('') || '<span class="muted">No genres yet</span>';

  container.querySelectorAll('.chip').forEach((chip) => {
    chip.onclick = () => {
      state.tag = state.tag === chip.dataset.tag ? '' : chip.dataset.tag;
      loadDiscover();
    };
  });
}

function renderCards(cards) {
  const container = $('#cards');
  const empty = $('#discover-empty');

  if (!cards.length) {
    container.innerHTML = '';
    empty.hidden = false;
    empty.innerHTML = state.status === 'new'
      ? '<strong>Nothing here yet</strong>Hit “Scan now” to ask for recommendations.'
      : `<strong>No ${escapeHtml(state.status)} tracks</strong>Try a different filter.`;
    return;
  }

  empty.hidden = true;
  container.innerHTML = cards.map(cardHtml).join('');
  container.querySelectorAll('[data-action]').forEach((button) => {
    button.onclick = () => act(button.dataset.id, button.dataset.action, button);
  });
}

function cardHtml(card) {
  const cover = card.cover_url
    ? `<img src="${escapeHtml(card.cover_url)}" alt="" loading="lazy"
            onerror="this.replaceWith(Object.assign(document.createElement('span'),
                     {className:'initials',textContent:'${escapeHtml(initials(card.artist))}'}))">`
    : `<span class="initials">${escapeHtml(initials(card.artist))}</span>`;

  const badge = {
    saved: '<span class="badge saved">Saved</span>',
    downloaded: '<span class="badge done">Downloaded</span>',
    failed: '<span class="badge failed">Failed</span>',
    queued: '<span class="badge">Queued</span>',
    downloading: '<span class="badge">Downloading</span>',
    hidden: '<span class="badge">Hidden</span>',
  }[card.status] || '';

  const meta = [card.album, card.year].filter(Boolean).map(escapeHtml).join(' · ');
  const tags = card.tags.slice(0, 3)
    .map((tag) => `<span>${escapeHtml(tag)}</span>`).join('');

  const downloadable = !['downloaded', 'queued', 'downloading'].includes(card.status);

  return `
    <article class="card" id="card-${card.id}">
      <div class="cover">
        ${cover}
        ${badge}
        <span class="match ${tier(card.match)}">${card.match}%</span>
      </div>
      <div class="card-body">
        <div class="card-title">${escapeHtml(card.title)}</div>
        <div class="card-artist">${escapeHtml(card.artist)}</div>
        ${meta ? `<div class="card-meta">${meta}</div>` : ''}
        ${card.reason ? `<div class="card-reason">${escapeHtml(card.reason)}</div>` : ''}
        ${card.error ? `<div class="card-error">${escapeHtml(card.error)}</div>` : ''}
        ${tags ? `<div class="card-tags">${tags}</div>` : ''}
      </div>
      <div class="card-actions">
        <button class="btn btn-icon" data-id="${card.id}" data-action="download"
                title="Download as MP3 320" ${downloadable ? '' : 'disabled'}>↓</button>
        <button class="btn btn-icon ${card.status === 'saved' ? 'is-on' : ''}"
                data-id="${card.id}" data-action="${card.status === 'saved' ? 'unsave' : 'save'}"
                title="Save for later">♥</button>
        <button class="btn btn-icon" data-id="${card.id}"
                data-action="${card.status === 'hidden' ? 'unhide' : 'hide'}"
                title="${card.status === 'hidden' ? 'Un-hide' : 'Never suggest again'}">✕</button>
      </div>
    </article>`;
}

async function act(id, action, button) {
  const card = $(`#card-${id}`);
  if (card) card.classList.add('is-busy');
  button.disabled = true;

  try {
    await api(`/suggestions/${id}/${action}`, { method: 'POST' });
    if (action === 'download') {
      toast('Queued for download');
      startFastPoll();
    }
    await loadDiscover();
  } catch (error) {
    toast(error.message, true);
    if (card) card.classList.remove('is-busy');
    button.disabled = false;
  }
}

// ─── Downloads ──────────────────────────────────────────────────────────

async function loadDownloads() {
  const data = await api(`/downloads?status=${state.downloadStatus}`);
  const active = new Map(data.active.map((item) => [item.id, item]));
  const body = $('#downloads-table tbody');
  const empty = $('#downloads-empty');

  $('#downloads-table').hidden = !data.downloads.length;
  empty.hidden = data.downloads.length > 0;

  const done = data.downloads.filter((row) => row.status === 'done');
  const failed = data.downloads.filter((row) => row.status === 'failed');
  const total = done.reduce((sum, row) => sum + (row.bytes || 0), 0);
  $('#download-summary').textContent =
    `${done.length} downloaded · ${bytes(total)}`
    + (failed.length ? ` · ${failed.length} failed` : '')
    + (data.active.length ? ` · ${data.active.length} in flight` : '');
  $('#retry-failed').hidden = failed.length === 0;

  body.innerHTML = data.downloads.map((row) => {
    const live = active.get(row.id);
    const status = live
      ? `<span class="pill ${row.status}">${row.status}</span>
         <div class="progress"><i style="width:${live.progress}%"></i></div>`
      : `<span class="pill ${row.status}">${row.status}</span>`;

    return `
      <tr>
        <td><strong>${escapeHtml(row.title)}</strong><br><span class="muted">${escapeHtml(row.artist)}</span></td>
        <td>${escapeHtml(row.album || '—')}</td>
        <td class="num">${row.match != null ? `${row.match}%` : '—'}</td>
        <td>${status}${row.error ? `<div class="card-error">${escapeHtml(row.error)}</div>` : ''}</td>
        <td class="path" title="${escapeHtml(row.path)}">${escapeHtml(row.path || '—')}</td>
        <td class="num">${bytes(row.bytes)}</td>
        <td>
          ${row.status === 'failed' ? `<button class="btn btn-icon" data-retry="${row.id}" title="Try again">↻</button>` : ''}
          <button class="btn btn-icon" data-remove="${row.id}" title="Remove">🗑</button>
        </td>
      </tr>`;
  }).join('');

  body.querySelectorAll('[data-retry]').forEach((button) => {
    button.onclick = async () => {
      await api(`/downloads/${button.dataset.retry}/retry`, { method: 'POST' });
      startFastPoll();
      loadDownloads();
    };
  });

  body.querySelectorAll('[data-remove]').forEach((button) => {
    button.onclick = async () => {
      const alsoFile = confirm('Delete the downloaded file from disk as well?');
      await api(`/downloads/${button.dataset.remove}?delete_file=${alsoFile}`, { method: 'DELETE' });
      loadDownloads();
    };
  });

  if (data.active.length) startFastPoll();
}

// ─── Stats ──────────────────────────────────────────────────────────────

async function loadStats() {
  const data = await api(`/stats?days=${state.days}`);

  $('#stat-tiles').innerHTML = [
    ['Plays', data.plays.toLocaleString()],
    ['Artists', data.artists.toLocaleString()],
    ['Distinct tracks', data.tracks.toLocaleString()],
    ['Downloaded', `${data.downloaded} · ${bytes(data.downloaded_bytes)}`],
    ['New vs familiar', percentSplit(data.new_plays, data.familiar_plays)],
  ].map(([label, value]) => `
    <div class="tile"><div class="value">${escapeHtml(value)}</div><div class="label">${label}</div></div>
  `).join('');

  drawChart('#chart-daily', data.daily.map((day) => day.plays),
            data.daily.length ? [data.daily[0].day, data.daily[data.daily.length - 1].day] : []);
  drawChart('#chart-clock', data.clock.map((hour) => hour.plays), ['00:00', '12:00', '23:00']);

  $('#top-artists').innerHTML = rankedList(data.top_artists, (row) => row.artist);
  $('#top-tracks').innerHTML = rankedList(
    data.top_tracks, (row) => `${row.artist} — ${row.title}`);

  loadSummary(false);
}

function percentSplit(fresh, familiar) {
  const total = fresh + familiar;
  return total ? `${Math.round((fresh / total) * 100)}% new` : '—';
}

function rankedList(rows, label) {
  if (!rows.length) return '<li class="muted">Nothing yet</li>';
  return rows.map((row) => `
    <li><span class="name">${escapeHtml(label(row))}</span><span class="plays">${row.plays}</span></li>
  `).join('');
}

function drawChart(selector, values, axis) {
  const peak = Math.max(1, ...values);
  $(selector).innerHTML = values
    .map((value) => `<div class="bar" title="${value}"><i style="height:${(value / peak) * 100}%"></i></div>`)
    .join('');

  const next = $(selector).nextElementSibling;
  if (next && next.classList.contains('chart-axis')) next.remove();
  if (axis.length) {
    const element = document.createElement('div');
    element.className = 'chart-axis';
    element.innerHTML = axis.map((label) => `<span>${escapeHtml(label)}</span>`).join('');
    $(selector).after(element);
  }
}

async function loadSummary(refresh) {
  const panel = $('#taste');
  if (refresh) panel.innerHTML = '<span class="label">Your taste</span>Thinking…';
  panel.hidden = false;

  try {
    const data = await api(`/stats/summary?days=${state.days}&refresh=${refresh}`);
    if (!data.enabled) { panel.hidden = true; return; }
    panel.innerHTML = `<span class="label">Your taste</span>${
      escapeHtml(data.text || data.error || 'No summary yet.')}`;
  } catch (error) {
    panel.innerHTML = `<span class="label">Your taste</span>${escapeHtml(error.message)}`;
  }
}

// ─── Settings ───────────────────────────────────────────────────────────

function renderSettings(settings) {
  state.settings = settings;

  $$('[data-setting]').forEach((input) => {
    const value = settings[input.dataset.setting];
    if (input.type === 'checkbox') input.checked = Boolean(value);
    else input.value = value;
    syncOutput(input);
  });
}

function syncOutput(input) {
  const output = $(`[data-output="${input.dataset.setting}"]`);
  if (output) output.textContent = input.value;
}

async function saveSetting(input) {
  const key = input.dataset.setting;
  const value = input.type === 'checkbox' ? input.checked : input.value;
  try {
    const data = await api('/settings', {
      method: 'PUT',
      body: JSON.stringify({ [key]: value }),
    });
    state.settings = data.settings;
    toast('Saved');
    if (key === 'min_match' || key === 'sort') refreshStatus();
  } catch (error) {
    toast(error.message, true);
  }
}

function renderConnections(status) {
  const rows = status.history.sources.map((source) => {
    const label = source.name === 'lastfm' ? 'Last.fm' : 'ListenBrainz';
    if (!source.configured) return [label, 'not configured', 'off'];
    if (source.error) return [label, source.error, 'err'];
    return [label, `synced ${ago(source.synced_at)}`, 'ok'];
  });

  rows.push(
    ['AI backend', `${status.ai.provider} · ${status.ai.model}`, status.ai.available ? 'ok' : 'err'],
    ['Plays stored', status.history.total_plays.toLocaleString(), 'ok'],
    ['Music directory', status.music_dir, 'ok'],
    ['Exclusion folder', status.exclude_dir || 'not set', status.exclude_dir ? 'ok' : 'off'],
  );

  $('#connections').innerHTML = rows.map(([term, value, cls]) => `
    <div><dt>${escapeHtml(term)}</dt><dd class="${cls}">${escapeHtml(value)}</dd></div>
  `).join('');
}

// ─── Status polling ─────────────────────────────────────────────────────

async function refreshStatus() {
  let status;
  try {
    status = await api('/status');
  } catch {
    $('#scan-state').textContent = 'offline';
    return;
  }

  const wasScanning = state.scanning;
  state.scanning = status.scan.running;

  const label = $('#scan-state');
  const bar = $('#scan-progress');
  label.classList.toggle('is-running', state.scanning);
  bar.hidden = !state.scanning;

  if (state.scanning) {
    const { step, done, total } = status.scan;
    // Only the enrichment phase has a countable unit of work. Everything
    // before it (syncing, indexing, waiting on the model) has no meaningful
    // percentage, so the bar sweeps rather than inventing one.
    const countable = total > 0 && done > 0;
    bar.classList.toggle('is-indeterminate', !countable);
    bar.firstElementChild.style.width = countable ? `${Math.round((done / total) * 100)}%` : '';
    label.textContent = countable ? `${step} — ${done}/${total}` : step;
  } else if (status.scan.last) {
    const last = status.scan.last;
    label.textContent = last.status === 'failed'
      ? `last scan failed: ${last.error}`
      : `last scan ${ago(last.finished_at || last.started_at)} · ${last.kept} kept`;
  } else {
    label.textContent = 'never scanned';
  }

  $('#scan-now').disabled = state.scanning;
  renderSettings(status.settings);
  renderConnections(status);
  showSetupBanner(status);

  if (wasScanning && !state.scanning) {
    toast('Scan finished');
    if (state.tab === 'discover') loadDiscover();
  }
  if (wasScanning !== state.scanning) setStatusPoll();
}

/** Poll quickly while a scan runs so the bar moves, slowly when idle. */
function setStatusPoll() {
  clearInterval(state.statusPoll);
  state.statusPoll = setInterval(refreshStatus, state.scanning ? 1500 : 5000);
}

function showSetupBanner(status) {
  const banner = $('#banner');
  const problems = [];

  if (!status.history.sources.some((source) => source.configured)) {
    problems.push('No listening history configured — set LASTFM_API_KEY and LASTFM_USER, or LISTENBRAINZ_USER, in .env.');
  }
  if (!status.ai.available) {
    problems.push(`The ${status.ai.provider} backend is not configured — set its key or URL in .env.`);
  }

  banner.hidden = !problems.length;
  banner.textContent = problems.join(' ');
}

function startFastPoll() {
  if (state.fastPoll) return;
  state.fastPoll = setInterval(async () => {
    const { active } = await api('/downloads/active').catch(() => ({ active: [] }));
    if (state.tab === 'downloads') loadDownloads();
    if (!active.length) {
      clearInterval(state.fastPoll);
      state.fastPoll = null;
      if (state.tab === 'discover') loadDiscover();
    }
  }, 2000);
}

// ─── Wiring ─────────────────────────────────────────────────────────────

function showTab(name) {
  state.tab = name;
  $$('.tab').forEach((tab) => tab.classList.toggle('is-active', tab.dataset.tab === name));
  $$('.panel').forEach((panel) => panel.classList.toggle('is-active', panel.id === `tab-${name}`));

  ({
    discover: loadDiscover,
    downloads: loadDownloads,
    stats: loadStats,
    settings: refreshStatus,
  }[name])().catch((error) => toast(error.message, true));
}

function init() {
  $$('.tab').forEach((tab) => { tab.onclick = () => showTab(tab.dataset.tab); });

  $('#scan-now').onclick = async () => {
    try {
      await api('/scan', { method: 'POST' });
      state.scanning = true;
      $('#scan-now').disabled = true;
      toast('Scan started');
      refreshStatus();
    } catch (error) {
      toast(error.message, true);
    }
  };

  $('#f-status').onchange = (event) => { state.status = event.target.value; loadDiscover(); };
  $('#f-sort').onchange = (event) => { state.sort = event.target.value; loadDiscover(); };
  $('#f-match').oninput = (event) => { $('#f-match-value').textContent = `${event.target.value}%`; };
  $('#f-match').onchange = (event) => { state.minMatch = Number(event.target.value); loadDiscover(); };

  $('#download-visible').onclick = async () => {
    if (!confirm(`Queue every new track at ${state.minMatch}% or above?`)) return;
    const data = await api('/suggestions/download-all', {
      method: 'POST',
      body: JSON.stringify({ min_match: state.minMatch }),
    });
    toast(`Queued ${data.queued} downloads`);
    startFastPoll();
    loadDiscover();
  };

  $('#d-status').onchange = (event) => { state.downloadStatus = event.target.value; loadDownloads(); };

  $('#paste-form').onsubmit = async (event) => {
    event.preventDefault();
    const input = $('#paste-url');
    const url = input.value.trim();
    if (!url) return;

    const button = event.target.querySelector('button');
    button.disabled = true;
    try {
      const data = await api('/downloads/url', {
        method: 'POST',
        body: JSON.stringify({ url }),
      });
      input.value = '';
      toast(data.matched
        ? `Queued ${data.artist} — ${data.title}`
        : `Queued ${data.artist} — ${data.title}, finding it on YouTube Music`);
      startFastPoll();
      loadDownloads();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  };

  $('#retry-failed').onclick = async () => {
    const data = await api('/downloads/retry-failed', { method: 'POST' });
    toast(data.queued ? `Requeued ${data.queued} downloads` : 'Nothing to retry');
    startFastPoll();
    loadDownloads();
  };
  $('#s-days').onchange = (event) => { state.days = Number(event.target.value); loadStats(); };
  $('#refresh-summary').onclick = () => loadSummary(true);

  $$('[data-setting]').forEach((input) => {
    input.oninput = () => syncOutput(input);
    input.onchange = () => saveSetting(input);
  });

  refreshStatus();
  showTab('discover');
  setStatusPoll();
}

document.addEventListener('DOMContentLoaded', init);
