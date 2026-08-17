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
  // The rows the server last sent, kept so searching and re-sorting the
  // downloads table are instant and do not cost a request.
  downloadRows: [],
  downloadActive: new Map(),
  downloadSearch: '',
  downloadSort: { key: 'newest', dir: 'desc' },
  days: 90,
  settings: {},
  scanning: false,
  fastPoll: null,
  statusPoll: null,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

const reducedMotion = () =>
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/** Stagger a freshly rendered list in. Capped, so a 200-row table does not
 *  spend four seconds arriving. */
function animateIn(elements) {
  if (reducedMotion()) return;
  elements.forEach((element, index) => {
    element.style.animationDelay = `${Math.min(index, 14) * 16}ms`;
    element.classList.add('is-entering');
  });
}

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
  clearTimeout(toast.hideTimer);
  // Unhiding and adding the class in the same frame would skip the transition,
  // because the element has no rendered "before" state to move from.
  requestAnimationFrame(() => element.classList.add('is-on'));
  toast.timer = setTimeout(() => {
    element.classList.remove('is-on');
    toast.hideTimer = setTimeout(() => { element.hidden = true; }, 220);
  }, 4000);
}

// ─── Confirmation dialog ────────────────────────────────────────────────

/**
 * The in-app stand-in for window.confirm(). Resolves to
 * `{ ok, checked }` — `checked` is the state of the optional extra choice,
 * which is how the delete dialog asks about the file on disk in the same
 * breath as asking about the row.
 */
function ask({ title, body, confirm = 'Confirm', danger = false, option = '' }) {
  const modal = $('#modal');
  const confirmButton = $('#modal-confirm');
  const checkbox = $('#modal-checkbox');
  const opener = document.activeElement;

  $('#modal-title').textContent = title;
  $('#modal-body').textContent = body;
  confirmButton.textContent = confirm;
  confirmButton.classList.toggle('btn-danger', danger);
  confirmButton.classList.toggle('btn-primary', !danger);
  $('#modal-option').hidden = !option;
  $('#modal-checkbox-label').textContent = option;
  checkbox.checked = false;

  // A dialog opened while the previous one is still fading out would otherwise
  // be hidden by that one's pending timer.
  clearTimeout(ask.timer);
  modal.hidden = false;
  requestAnimationFrame(() => modal.classList.add('is-on'));
  confirmButton.focus();

  return new Promise((resolve) => {
    const focusable = () =>
      Array.from(modal.querySelectorAll('button, input'))
        .filter((element) => element.offsetParent !== null);

    const onKey = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        close(false);
        return;
      }
      if (event.key !== 'Tab') return;
      // Tab must not walk out of a modal dialog and into the page behind it.
      const items = focusable();
      const edge = event.shiftKey ? items[0] : items[items.length - 1];
      if (document.activeElement === edge) {
        event.preventDefault();
        (event.shiftKey ? items[items.length - 1] : items[0]).focus();
      }
    };

    const close = (ok) => {
      modal.classList.remove('is-on');
      modal.removeEventListener('keydown', onKey);
      ask.timer = setTimeout(() => { modal.hidden = true; }, 180);
      if (opener && opener.isConnected) opener.focus();
      resolve({ ok, checked: checkbox.checked });
    };

    modal.addEventListener('keydown', onKey);
    $('#modal-cancel').onclick = () => close(false);
    $('#modal-backdrop').onclick = () => close(false);
    confirmButton.onclick = () => close(true);
  });
}

/** Musicdrome is usually served over plain HTTP on a LAN, where the async
 *  clipboard API is unavailable. Fall back to the old selection trick. */
async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch { /* fall through */ }
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.appendChild(area);
  area.select();
  let copied = false;
  try { copied = document.execCommand('copy'); } catch { copied = false; }
  area.remove();
  return copied;
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

// What YouTube served, and whether the file still holds those exact bytes.
// "copied" is the good outcome and the usual one; "converted" means the track
// was served in a format other than the configured one and was re-encoded.
function audio(row) {
  if (!row.source_codec) return '<span class="muted">—</span>';
  const rate = row.source_abr ? ` ${row.source_abr}k` : '';
  const label = escapeHtml(row.source_codec + rate);
  if (!row.encoded) return `<span class="muted">${label}</span>`;
  // Narrowed to the two known values rather than escaped: this reaches a class
  // attribute as well as the text, and only these two mean anything in either.
  const state = row.encoded === 'copied' ? 'copied' : 'converted';
  const title = state === 'copied'
    ? 'Copied from the source stream without re-encoding'
    : 'Re-encoded: the source was served in another format';
  return `${label} <span class="pill ${state}" title="${title}">${state}</span>`;
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
  animateIn(Array.from(container.children));
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

  // A match the Navidrome hearts moved says so, and says by how much. The
  // number on its own would be indistinguishable from one the model produced
  // unaided, which would make the second signal impossible to check.
  const boosted = Number(card.affinity) > 0;
  const matchTitle = boosted
    ? `${card.match_base}% from the model, +${card.affinity} from what you heart`
      + (card.affinity_reason ? ` — ${card.affinity_reason}` : '')
    : `${card.match}% match`;

  return `
    <article class="card" id="card-${card.id}">
      <div class="cover">
        ${cover}
        ${badge}
        <span class="match ${tier(card.match)}${boosted ? ' is-hearted' : ''}"
              title="${escapeHtml(matchTitle)}">${card.match}%${boosted ? '<i>♥</i>' : ''}</span>
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

const basename = (path) => String(path || '').split('/').pop();

// What each sortable column sorts by. Everything is reduced to a string or a
// number here so one comparator can handle the lot.
const SORT_KEYS = {
  newest: (row) => row.created_at || 0,
  track: (row) => `${row.artist} ${row.title}`.toLowerCase(),
  album: (row) => (row.album || '').toLowerCase(),
  match: (row) => (row.match == null ? -1 : row.match),
  // Ordered by what wants attention rather than alphabetically: in flight,
  // then waiting, then broken, then finished and forgettable.
  status: (row) => {
    const rank = ['downloading', 'queued', 'failed', 'done'].indexOf(row.status);
    return rank < 0 ? 9 : rank;
  },
  file: (row) => basename(row.path).toLowerCase(),
  audio: (row) => `${row.source_codec || ''} ${String(row.source_abr || 0).padStart(4, '0')}`,
  size: (row) => row.bytes || 0,
};

// Which way round a column reads first: names from the top, quantities from
// the largest.
const SORT_DIR = {
  newest: 'desc', track: 'asc', album: 'asc', status: 'asc',
  file: 'asc', audio: 'asc', match: 'desc', size: 'desc',
};

async function loadDownloads({ animate = true } = {}) {
  if (!state.downloadRows.length) showDownloadSkeleton();

  const data = await api(
    `/downloads?${new URLSearchParams({ status: state.downloadStatus })}`);

  state.downloadRows = data.downloads;
  state.downloadActive = new Map(data.active.map((item) => [item.id, item]));

  const done = data.downloads.filter((row) => row.status === 'done');
  const failed = data.downloads.filter((row) => row.status === 'failed');
  const total = done.reduce((sum, row) => sum + (row.bytes || 0), 0);
  $('#download-summary').textContent =
    `${done.length} downloaded · ${bytes(total)}`
    + (failed.length ? ` · ${failed.length} failed` : '')
    + (data.active.length ? ` · ${data.active.length} in flight` : '');
  $('#retry-failed').hidden = failed.length === 0;

  renderDownloads({ animate });
  if (data.active.length) startFastPoll();
}

/** Filter, sort and draw from what the last request returned. Called without a
 *  request when the search box or a column heading changes. */
function renderDownloads({ animate = true } = {}) {
  const body = $('#downloads-table tbody');
  const empty = $('#downloads-empty');
  const needle = state.downloadSearch.trim().toLowerCase();

  const matching = needle
    ? state.downloadRows.filter((row) =>
      `${row.title} ${row.artist} ${row.album} ${row.path} ${row.status}`
        .toLowerCase().includes(needle))
    : state.downloadRows;

  const rows = sortDownloads(matching);

  $('.table-scroll').hidden = rows.length === 0;
  empty.hidden = rows.length > 0;
  if (!rows.length) {
    empty.textContent = state.downloadRows.length
      ? `Nothing matches “${state.downloadSearch.trim()}”.`
      : 'Nothing downloaded yet.';
  }

  $$('#downloads-table th[data-sort]').forEach((th) => {
    th.setAttribute('aria-sort', th.dataset.sort === state.downloadSort.key
      ? (state.downloadSort.dir === 'asc' ? 'ascending' : 'descending')
      : 'none');
  });

  body.innerHTML = rows.map(downloadRowHtml).join('');
  if (animate) animateIn(Array.from(body.children));
  wireDownloadRows(body);
}

function sortDownloads(rows) {
  const pick = SORT_KEYS[state.downloadSort.key] || SORT_KEYS.newest;
  const sign = state.downloadSort.dir === 'asc' ? 1 : -1;
  // Decorated so equal keys keep the server's own newest-first order rather
  // than shuffling between renders.
  return rows
    .map((row, index) => ({ row, index, key: pick(row) }))
    .sort((a, b) => {
      if (a.key < b.key) return -sign;
      if (a.key > b.key) return sign;
      return a.index - b.index;
    })
    .map((entry) => entry.row);
}

function sortDownloadsBy(key) {
  const current = state.downloadSort;
  state.downloadSort = current.key === key
    ? { key, dir: current.dir === 'asc' ? 'desc' : 'asc' }
    : { key, dir: SORT_DIR[key] || 'asc' };
  renderDownloads();
}

// Marks a cell as having nothing to say, so the stacked layout can leave it
// out rather than print a labelled em-dash.
const blank = (isBlank) => (isBlank ? ' data-empty="1"' : '');

function downloadRowHtml(row) {
  const live = state.downloadActive.get(row.id);
  const album = row.album || '—';
  const status = `<span class="pill ${row.status}">${row.status}</span>`
    + (live ? `<div class="progress"><i style="width:${live.progress}%"></i></div>` : '')
    + (row.error
      ? `<div class="card-error" title="${escapeHtml(row.error)}">${escapeHtml(row.error)}</div>`
      : '');

  const file = row.path
    ? `<button type="button" class="path" data-copy="${escapeHtml(row.path)}"
               title="${escapeHtml(row.path)}">${escapeHtml(basename(row.path))}</button>`
    : '<span class="muted">—</span>';

  // The fold only earns its line when there is something in it: a failed
  // download has no codec, and a second em-dash under the first says nothing.
  const foldAudio = row.source_codec ? `<div class="fold fold-audio">${audio(row)}</div>` : '';
  const foldAlbum = row.album ? `<div class="fold fold-album">${escapeHtml(row.album)}</div>` : '';

  return `
    <tr data-id="${row.id}">
      <td class="cell-track" data-label="Track">
        <div class="t-title" title="${escapeHtml(row.title)}">${escapeHtml(row.title)}</div>
        <div class="t-artist" title="${escapeHtml(row.artist)}">${escapeHtml(row.artist)}</div>
        ${foldAlbum}
      </td>
      <td class="cell-album" data-label="Album"${blank(!row.album)}
          title="${escapeHtml(album)}">${escapeHtml(album)}</td>
      <td class="num" data-label="Match"${blank(row.match == null)}>${
        row.match != null ? `${row.match}%` : '—'}</td>
      <td class="cell-status" data-label="Status">${status}</td>
      <td class="cell-file" data-label="File"${blank(!row.path)}>${file}${foldAudio}</td>
      <td class="audio" data-label="Audio"${blank(!row.source_codec)}>${audio(row)}</td>
      <td class="num" data-label="Size"${blank(!row.bytes)}>${bytes(row.bytes)}</td>
      <td class="cell-actions" data-label="">
        ${row.status === 'failed'
          ? `<button class="btn btn-icon" data-retry="${row.id}" title="Try again">↻</button>`
          : ''}
        <button class="btn btn-icon" data-remove="${row.id}" title="Remove">🗑</button>
      </td>
    </tr>`;
}

function wireDownloadRows(body) {
  body.querySelectorAll('[data-copy]').forEach((button) => {
    button.onclick = async () => {
      const copied = await copyText(button.dataset.copy);
      toast(copied ? 'Path copied' : button.dataset.copy, !copied);
    };
  });

  body.querySelectorAll('[data-retry]').forEach((button) => {
    button.onclick = async () => {
      try {
        await api(`/downloads/${button.dataset.retry}/retry`, { method: 'POST' });
        startFastPoll();
        loadDownloads();
      } catch (error) {
        toast(error.message, true);
      }
    };
  });

  body.querySelectorAll('[data-remove]').forEach((button) => {
    button.onclick = async () => {
      const id = Number(button.dataset.remove);
      const row = state.downloadRows.find((item) => item.id === id);
      const answer = await ask({
        title: 'Remove this download?',
        body: row
          ? `${row.artist} — ${row.title} will be taken off the list.`
          : 'This download will be taken off the list.',
        // Only worth asking about when there is a file to delete.
        option: row && row.path ? 'Also delete the file from disk' : '',
        confirm: 'Remove',
        danger: true,
      });
      if (!answer.ok) return;

      try {
        await api(`/downloads/${id}?delete_file=${answer.checked}`, { method: 'DELETE' });
        toast(answer.checked ? 'Removed, and deleted from disk' : 'Removed from the list');
        loadDownloads();
      } catch (error) {
        toast(error.message, true);
      }
    };
  });
}

/** Something to look at for the width of one request, so switching to the tab
 *  does not flash an empty frame first. */
function showDownloadSkeleton(count = 6) {
  $('.table-scroll').hidden = false;
  $('#downloads-empty').hidden = true;
  $('#downloads-table tbody').innerHTML = Array.from({ length: count }, () => `
    <tr class="skeleton-row" aria-hidden="true">
      <td><div class="skeleton skeleton-line" style="width:70%"></div>
          <div class="skeleton skeleton-line" style="width:40%;margin-top:.35rem"></div></td>
      <td><div class="skeleton skeleton-line" style="width:80%"></div></td>
      <td><div class="skeleton skeleton-line" style="width:60%"></div></td>
      <td><div class="skeleton skeleton-line" style="width:50%"></div></td>
      <td><div class="skeleton skeleton-line" style="width:85%"></div></td>
      <td><div class="skeleton skeleton-line" style="width:70%"></div></td>
      <td><div class="skeleton skeleton-line" style="width:55%"></div></td>
      <td><div class="skeleton skeleton-line" style="width:60%"></div></td>
    </tr>`).join('');
}

// ─── Stats ──────────────────────────────────────────────────────────────

async function loadStats() {
  const data = await api(`/stats?${new URLSearchParams({ days: state.days })}`);

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
    const data = await api(
      `/stats/summary?${new URLSearchParams({ days: state.days, refresh })}`);
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

  const navidrome = status.history.navidrome || {};
  if (!navidrome.configured) {
    rows.push(['Navidrome', 'not configured', 'off']);
  } else if (navidrome.error) {
    rows.push(['Navidrome', navidrome.error, 'err']);
  } else if (!navidrome.synced_at) {
    rows.push(['Navidrome', 'configured — syncs on the next scan', 'ok']);
  } else {
    rows.push([
      'Navidrome',
      `${navidrome.hearts.toLocaleString()} hearted of `
        + `${navidrome.tracks.toLocaleString()}, synced ${ago(navidrome.synced_at)}`,
      'ok',
    ]);
  }

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

  // First, because it is the one that silently wastes a whole scan: everything
  // else works and every download dies on the final write.
  if (status.music_dir_problem) {
    problems.push(`Downloads cannot be saved. ${status.music_dir_problem}`);
  }
  if (!status.history.sources.some((source) => source.configured)) {
    problems.push('No listening history configured — set LASTFM_USER or LISTENBRAINZ_USER in .env.');
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
    // Without `animate: false` the whole table would re-enter every two
    // seconds for as long as anything is downloading.
    if (state.tab === 'downloads') loadDownloads({ animate: false });
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
    const answer = await ask({
      title: 'Download everything shown?',
      body: `Every new suggestion matching ${state.minMatch}% or better will be `
        + 'queued for download.',
      confirm: 'Queue them',
    });
    if (!answer.ok) return;

    try {
      const data = await api('/suggestions/download-all', {
        method: 'POST',
        body: JSON.stringify({ min_match: state.minMatch }),
      });
      toast(`Queued ${data.queued} downloads`);
      startFastPoll();
      loadDiscover();
    } catch (error) {
      toast(error.message, true);
    }
  };

  $('#d-status').onchange = (event) => {
    state.downloadStatus = event.target.value;
    // A different server-side filter is a different set of rows, so the cached
    // ones must not be reused as the skeleton's stand-in.
    state.downloadRows = [];
    loadDownloads();
  };

  // Searching and sorting work on rows already in hand — no request, so the
  // table can keep up with typing.
  $('#d-search').oninput = (event) => {
    state.downloadSearch = event.target.value;
    renderDownloads({ animate: false });
  };

  $$('#downloads-table th[data-sort]').forEach((th) => {
    th.querySelector('.th-sort').onclick = () => sortDownloadsBy(th.dataset.sort);
  });

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

  measureTopbar();
  // The bar changes height on its own: it wraps to two lines on a narrow
  // window, and the scan label swaps short messages for long ones.
  if (window.ResizeObserver) new ResizeObserver(measureTopbar).observe($('.topbar'));
  else window.addEventListener('resize', measureTopbar);

  refreshStatus();
  showTab('discover');
  setStatusPoll();
}

/** The table header pins itself below the top bar, which is sticky and whose
 *  height changes when it wraps to a second line on a narrow window. */
function measureTopbar() {
  const bar = $('.topbar');
  if (!bar) return;
  document.documentElement.style.setProperty(
    '--topbar-h', `${Math.round(bar.getBoundingClientRect().height)}px`);
}

document.addEventListener('DOMContentLoaded', init);
