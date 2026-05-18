// Session-grouped activity table for the Journal UI.
//
// Owns: sessionMap (id → SessionState), the DOM mirror of session/conn rows,
// the two expansion levels (session row ▶ conn rows), and user prefs
// (sort order + caps) persisted to localStorage.
//
// Two-level column layout:
//   Session header (always visible): ▼ | session | connections | time range | time spent| bytes | tokens in | tokens out
//   Connection sub-table (nested inside each expanded session): ts | state (✓/✗/●) | active | method | path | model | classified | destination | new model | status | duration | bytes | tokens in | tokens out
//   Session row: ▼ | session | connections | time range | time spent | bytes | tokens in | tokens out
//
// Conn-table token cells are colour-coded by share of the model context
// window (MAX_CONTEXT_TOKENS): green minimal, yellow mid-high, red high.
// Session-row token cells are never coloured.

import { toggleExpansion, refreshExpansion } from './expand.js';
import { stateColor, stateGlyph } from './state-colors.js';

const PREFS_KEY = 'journal.ui.prefs';
const DEFAULT_PREFS = {
  maxSessions: 100,
  maxConnectionsPerSession: 100,
  sessionSort: 'newest',
  connectionSort: 'newest',
};

let prefs = loadPrefs();

function loadPrefs() {
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return { ...DEFAULT_PREFS };
    const parsed = JSON.parse(raw);
    return { ...DEFAULT_PREFS, ...parsed };
  } catch (_) {
    return { ...DEFAULT_PREFS };
  }
}

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
  } catch (err) {
    console.warn('Could not persist UI prefs', err);
  }
}

export function getPrefs() {
  return { ...prefs };
}

export function setPrefs(patch) {
  prefs = { ...prefs, ...patch };
  savePrefs();
  applyPrefsChange();
}

function formatTs(v) {
  if (!v) return null;
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return null;
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  const ms = String(d.getMilliseconds()).padStart(3, '0');
  return `${hh}:${mm}:${ss}.${ms}`;
}

// Soft green / yellow / red bucket for an HTTP status code.
export function statusClass(code) {
  const n = Number(code);
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n >= 500) return 'status-5xx';
  if (n >= 400) return 'status-4xx';
  if (n >= 300) return 'status-3xx';
  if (n >= 200) return 'status-2xx';
  return '';
}

const STATUS_CLASSES = ['status-2xx', 'status-3xx', 'status-4xx', 'status-5xx'];

// Model context window used to scale the conn-table token colour buckets.
const MAX_CONTEXT_TOKENS = 200000;
const CTX_CLASSES = ['ctx-low', 'ctx-mid', 'ctx-high'];

// Raw token count for a connection entry ('input' | 'output'), or null.
function tokenCount(entry, kind) {
  const t = entry.tokens;
  if (!t) return null;
  return t[kind] || 0;
}

function renderTokenColumn(entry, kind) {
  const n = tokenCount(entry, kind);
  if (n === null) return null;
  return n.toLocaleString();
}

// Green minimal / yellow mid-high / red high-very high, by share of context.
function ctxClass(n) {
  if (!Number.isFinite(n) || n <= 0) return '';
  const ratio = n / MAX_CONTEXT_TOKENS;
  if (ratio >= 0.8) return 'ctx-high';
  if (ratio >= 0.5) return 'ctx-mid';
  return 'ctx-low';
}

// Operator-configured: classification chrome (the `classified` column here,
// the routing field in expand.js) is hidden unless the proxy enabled it.
// The flag is injected into index.html's <head> before this module loads.
function classificationEnabled() {
  return typeof window !== 'undefined' && window.__classificationEnabled === true;
}

// Human-readable duration: <1s as `123ms`, else `1.23s`.
function fmtDurationMs(n) {
  return n < 1000 ? `${Math.round(n)}ms` : `${(n / 1000).toFixed(2)}s`;
}

// SUCCESS / FAILURE are the closed states; the live timer stops there
// and the real duration_ms (received → completed) takes over.
function isTerminalState(state) {
  return state === 'SUCCESS' || state === 'FAILURE';
}

// Milliseconds elapsed since the connection entered the sending stage,
// or null if it hasn't reached that stage yet / is already closed /
// already has a final duration. Used both by the duration column render
// and the per-second ticker that keeps it advancing between SSE updates.
function liveElapsedMs(e) {
  if (!e || !e.sending_ts) return null;
  if (isTerminalState(e.state)) return null;
  const ms = e.duration_ms;
  if (ms !== null && ms !== undefined && ms !== '') return null;
  const start = new Date(e.sending_ts).getTime();
  if (!Number.isFinite(start)) return null;
  const elapsed = Date.now() - start;
  return elapsed >= 0 ? elapsed : null;
}

// Connection-level columns (shown inside an expanded session's sub-table).
const ALL_COLUMNS = [
  { key: 'ts',              label: 'ts',          render: (e) => formatTs(e.ts) },
  { key: 'state',           label: 'state',       render: (e) => e.state },
  { key: 'active',          label: 'active',      render: (e) => (e.entry_idx != null && e.entry_idx >= 0) ? e.entry_idx + 1 : null },
  { key: 'method',          label: 'method',      render: (e) => e.method },
  { key: 'path',            label: 'path',        render: (e) => e.path },
  { key: 'original_model',  label: 'model',       render: (e) => e.original_model },
  { key: 'classified',      label: 'classified',  render: (e) => e.classified },
  { key: 'destination_url', label: 'destination', render: (e) => e.destination_url },
  { key: 'new_model',       label: 'destination model',   render: (e) => e.new_model },
  { key: 'status',          label: 'status',      render: (e) => e.status },
  { key: 'duration',        label: 'duration',    render: (e) => {
    const ms = e.duration_ms;
    if (ms !== null && ms !== undefined && ms !== '') {
      const n = Number(ms);
      if (Number.isFinite(n)) return fmtDurationMs(n);
    }
    const live = liveElapsedMs(e);
    if (live !== null) return fmtDurationMs(live);
    return null;
  }},
  { key: 'bytes',           label: 'bytes',       render: (e) => e.bytes },
  { key: 'tokens_in',       label: 'tokens in',   render: (e) => renderTokenColumn(e, 'input') },
  { key: 'tokens_out',      label: 'tokens out',  render: (e) => renderTokenColumn(e, 'output') },
];
const COLUMNS = ALL_COLUMNS.filter(
  (c) => c.key !== 'classified' || classificationEnabled());

// Session-level columns (always visible).
const SESSION_COLUMNS = [
  { key: 'caret' },
  { key: 'id' },
  { key: 'count' },
  { key: 'time_range' },
  // time spent column: sum of connection durations
  { key: 'time_spent' },
  { key: 'bytes' },
  { key: 'tokens_in' },
  { key: 'tokens_out' },
];

function renderCountCell(count) {
  return `${count} conn${count === 1 ? '' : 's'}`;
}

function renderTimeRangeCell(firstTs, lastTs) {
  return `${fmtTs(firstTs)} → ${fmtTs(lastTs)}`;
}

const NUM_CONN_COLUMNS = COLUMNS.length;
const NUM_SESSION_COLUMNS = SESSION_COLUMNS.length;

function sessionMapInit() {
  return new Map();
}

let sessionMap = new Map();
let tbody = null;

function makeSessionState(sessionId, entry) {
  return {
    id: sessionId,
    firstTs: entry.ts || null,
    lastTs: entry.ts || null,
    count: 0,
    bytes: 0,
    inputTokens: 0,
    outputTokens: 0,
    totalDurationMs: 0,
    children: new Map(),       // entry_id → shallow entry
    expanded: false,
    sessionTr: null,
    childTrs: new Map(),
    connsTr: null,             // wrapper <tr class="session-conns-row">
    connsTbody: null,          // <tbody> inside the nested conn-table
  };
}

function fmtTs(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}

function buildSessionCell(state, col) {
  const td = document.createElement('td');
  td.className = `session-cell session-cell-${col.key}`;
  const { render } = SESSION_COLUMNS.find((c) => c.key === col.key);
  let value;
  switch (col.key) {
    case 'caret':
      td.textContent = state.expanded ? '▼' : '▶';
      td.style.opacity = '0.75';
      break;
    case 'id':
      td.textContent = state.id;
      td.style.maxWidth = '20em';
      td.style.overflow = 'hidden';
      td.style.textOverflow = 'ellipsis';
      td.style.whiteSpace = 'nowrap';
      td.style.fontFamily = 'ui-monospace, SFMono-Regular, Menlo, monospace';
      td.style.fontSize = '0.8rem';
      td.style.opacity = '0.85';
      break;
    case 'count':
      td.textContent = renderCountCell(state.count);
      td.style.opacity = '0.7';
      break;
    case 'time_range':
      td.textContent = renderTimeRangeCell(state.firstTs, state.lastTs);
      td.style.opacity = '0.7';
      break;
    case 'time_spent':
      if (state.totalDurationMs <= 0) {
        td.textContent = '—';
        td.classList.add('dim');
      } else if (state.totalDurationMs < 1000) {
        td.textContent = `${Math.round(state.totalDurationMs)}ms`;
      } else {
        td.textContent = `${(state.totalDurationMs / 1000).toFixed(2)}s`;
      }
      td.style.opacity = '0.7';
      break;
    case 'bytes':
      td.textContent = `${state.bytes.toLocaleString()} bytes`;
      td.style.opacity = '0.7';
      break;
    case 'tokens_in':
      td.textContent = `${state.inputTokens.toLocaleString()} in`;
      td.style.opacity = '0.7';
      break;
    case 'tokens_out':
      td.textContent = `${state.outputTokens.toLocaleString()} out`;
      td.style.opacity = '0.7';
      break;
  }
  return td;
}

function buildSessionRow(state) {
  const tr = document.createElement('tr');
  tr.className = 'session-row';
  tr.dataset.sessionId = state.id;
  for (const col of SESSION_COLUMNS) {
    tr.appendChild(buildSessionCell(state, col));
  }
  tr.addEventListener('click', () => toggleSessionExpansion(state.id));
  return tr;
}

function refreshSessionRow(state) {
  if (!state.sessionTr) return;
  // Rebuild each cell in place.
  for (let i = 0; i < SESSION_COLUMNS.length; i++) {
    const col = SESSION_COLUMNS[i];
    const cell = state.sessionTr.children[i] || document.createElement('td');
    state.sessionTr.replaceChild(buildSessionCell(state, col), cell);
  }
}

function applyAggregateDelta(state, prev, next) {
  const prevBytes = (prev && prev.bytes) || 0;
  const nextBytes = (next && next.bytes) || 0;
  state.bytes += nextBytes - prevBytes;

  const prevIn = (prev && prev.tokens && prev.tokens.input) || 0;
  const nextIn = (next && next.tokens && next.tokens.input) || 0;
  state.inputTokens += nextIn - prevIn;

  const prevOut = (prev && prev.tokens && prev.tokens.output) || 0;
  const nextOut = (next && next.tokens && next.tokens.output) || 0;
  state.outputTokens += nextOut - prevOut;

  // Track total duration delta across children
  const prevDur = prev
    ? (prev.duration_ms || 0)
    : 0;
  const nextDur = next
    ? (next.duration_ms || 0)
    : 0;
  // Only count positive durations
  if (nextDur > 0) state.totalDurationMs += nextDur - prevDur;
  // Ensure non-negative
  if (state.totalDurationMs < 0) state.totalDurationMs = 0;

  if (next && next.ts) {
    if (!state.firstTs || next.ts < state.firstTs) state.firstTs = next.ts;
    if (!state.lastTs || next.ts > state.lastTs) state.lastTs = next.ts;
  }
}

function insertSessionRowSorted(state) {
  if (!tbody) return;
  if (prefs.sessionSort === 'newest') {
    tbody.prepend(state.sessionTr);
  } else {
    tbody.appendChild(state.sessionTr);
  }
}

function evictSurplusSessions() {
  while (sessionMap.size > prefs.maxSessions) {
    let oldest = null;
    for (const s of sessionMap.values()) {
      if (!oldest || (s.firstTs || '') < (oldest.firstTs || '')) oldest = s;
    }
    if (!oldest) break;
    removeSessionDom(oldest);
    sessionMap.delete(oldest.id);
  }
}

function removeSessionDom(state) {
  if (state.connsTr && state.connsTr.parentNode) {
    state.connsTr.parentNode.removeChild(state.connsTr);
  }
  state.childTrs.clear();
  state.connsTr = null;
  state.connsTbody = null;
  if (state.sessionTr && state.sessionTr.parentNode) {
    state.sessionTr.parentNode.removeChild(state.sessionTr);
  }
  state.sessionTr = null;
}

function buildConnRow(state, entry) {
  const tr = document.createElement('tr');
  tr.className = 'conn-row';
  tr.dataset.id = entry.id;
  tr.dataset.parentSession = state.id;
  for (let i = 0; i < NUM_CONN_COLUMNS; i++) {
    const td = document.createElement('td');
    // Match the <th>'s per-column class so column width / alignment /
    // ellipsis CSS (which targets td.conn-table-col-*) actually applies.
    td.className = 'conn-table-col-' + COLUMNS[i].key;
    tr.appendChild(td);
  }
  writeConnCells(tr, entry);
  tr.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleExpansion(tr, entry.id);
  });
  return tr;
}

function writeConnCells(tr, entry) {
  const tds = tr.children;
  for (let i = 0; i < COLUMNS.length; i++) {
    const { key, render } = COLUMNS[i];
    const v = render(entry, key);
    const td = tds[i];
    if (v === null || v === undefined) {
      td.textContent = '—';
      td.classList.add('dim');
    } else {
      td.textContent = String(v);
      td.classList.remove('dim');
    }
    if (key === 'state') {
      const s = entry.state;
      if (s) {
        td.textContent = stateGlyph(s);
        td.style.color = stateColor(s);
        td.title = s;
        td.classList.remove('dim');
      } else {
        td.textContent = '—';
        td.style.color = '';
        td.removeAttribute('title');
        td.classList.add('dim');
      }
    }
    if (key === 'status') {
      td.classList.remove(...STATUS_CLASSES, 'status-error', 'status-pill');
      if (entry.error && (entry.status === null || entry.status === undefined)) {
        td.textContent = 'ERROR';
        td.classList.remove('dim');
        td.classList.add('status-error', 'status-pill');
      } else {
        const cls = statusClass(v);
        if (cls) td.classList.add(cls);
      }
    }
    if (key === 'tokens_in' || key === 'tokens_out') {
      td.classList.remove(...CTX_CLASSES);
      const kind = key === 'tokens_in' ? 'input' : 'output';
      const cls = ctxClass(tokenCount(entry, kind));
      if (cls) td.classList.add(cls);
    }
  }
}

function childOrder(state) {
  const arr = Array.from(state.children.values());
  if (prefs.connectionSort === 'newest') {
    return arr.slice().reverse();
  }
  return arr;
}

function insertConnRow(state, tr, idx) {
  const tb = state.connsTbody;
  if (!tb) return;
  const before = nthConnRow(tb, idx);
  tb.insertBefore(tr, before);
}

// idx counts only .conn-row elements (skips interleaved .expansion rows).
function nthConnRow(tb, n) {
  let count = 0;
  for (let cur = tb.firstChild; cur; cur = cur.nextSibling) {
    if (cur.classList && cur.classList.contains('conn-row')) {
      if (count === n) return cur;
      count += 1;
    }
  }
  return null;
}

function evictSurplusChildren(state) {
  while (state.children.size > prefs.maxConnectionsPerSession) {
    const firstKey = state.children.keys().next().value;
    if (firstKey === undefined) break;
    state.children.delete(firstKey);
    const tr = state.childTrs.get(firstKey);
    if (tr) {
      const exp = tr.nextElementSibling;
      if (exp && exp.classList && exp.classList.contains('expansion')
          && exp.parentNode) {
        exp.parentNode.removeChild(exp);
      }
      if (tr.parentNode) tr.parentNode.removeChild(tr);
    }
    state.childTrs.delete(firstKey);
  }
}

function buildConnsWrapperRow(state) {
  const tr = document.createElement('tr');
  tr.className = 'session-conns-row';
  tr.dataset.parentSession = state.id;

  const td = document.createElement('td');
  td.colSpan = NUM_SESSION_COLUMNS;

  const table = document.createElement('table');
  table.className = 'conn-table';

  const thead = document.createElement('thead');
  const htr = document.createElement('tr');
  for (const col of COLUMNS) {
    const th = document.createElement('th');
    th.textContent = col.label;
    th.className = 'conn-table-col-' + col.key;
    htr.appendChild(th);
  }
  thead.appendChild(htr);

  const tb = document.createElement('tbody');

  table.appendChild(thead);
  table.appendChild(tb);
  td.appendChild(table);
  tr.appendChild(td);

  state.connsTr = tr;
  state.connsTbody = tb;
  return tr;
}

function expandSession(state) {
  state.expanded = true;
  const wrapper = buildConnsWrapperRow(state);
  // Insert the wrapper row immediately after the session row.
  tbody.insertBefore(wrapper, state.sessionTr.nextSibling);
  const ordered = childOrder(state);
  for (let i = 0; i < ordered.length; i++) {
    const entry = ordered[i];
    const tr = buildConnRow(state, entry);
    state.childTrs.set(entry.id, tr);
    state.connsTbody.appendChild(tr);
  }
}

function collapseSession(state) {
  state.expanded = false;
  if (state.connsTr && state.connsTr.parentNode) {
    // Removing the wrapper row drops the whole nested table, including
    // any open per-connection deep-expand panels.
    state.connsTr.parentNode.removeChild(state.connsTr);
  }
  state.childTrs.clear(); // DOM mirror only; children data retained for aggregates
  state.connsTr = null;
  state.connsTbody = null;
}

// SSE only repaints a conn row when the backend pushes an update, but a
// connection sits silent in the sending/awaiting stage while the upstream
// model works. This ticker advances the duration cell of those in-flight
// rows once a second so the timer visibly counts up until close.
let liveTimer = null;

function tickLiveDurations() {
  for (const state of sessionMap.values()) {
    if (!state.expanded) continue;
    for (const e of state.children.values()) {
      const elapsed = liveElapsedMs(e);
      if (elapsed === null) continue;
      const tr = state.childTrs.get(e.id);
      if (!tr) continue;
      const cell = tr.querySelector('.conn-table-col-duration');
      if (!cell) continue;
      cell.textContent = fmtDurationMs(elapsed);
      cell.classList.remove('dim');
    }
  }
}

export function initSessionTable(tbodyEl) {
  tbody = tbodyEl;
  if (liveTimer === null && typeof setInterval === 'function') {
    liveTimer = setInterval(tickLiveDurations, 1000);
  }
}

export function upsertConnection(entry) {
  if (!entry || !entry.id) return;
  if (entry.type !== 'proxy_connection') return;
  const sid = entry.session_id || '<unknown>';

  let state = sessionMap.get(sid);
  const isNewSession = !state;
  if (isNewSession) {
    state = makeSessionState(sid, entry);
    sessionMap.set(sid, state);
  }

  if (!state || !state.children) {
    console.error('[session-table] invalid state for', sid);
    return;
  }

  const prev = state.children.get(entry.id);
  const isNewChild = !prev;
  if (isNewChild) state.count += 1;
  state.children.set(entry.id, entry);
  applyAggregateDelta(state, prev, entry);

  if (isNewSession) {
    state.sessionTr = buildSessionRow(state);
    insertSessionRowSorted(state);
    evictSurplusSessions();
  } else {
    refreshSessionRow(state);
  }

  if (state.expanded) {
    if (isNewChild) {
      const tr = buildConnRow(state, entry);
      state.childTrs.set(entry.id, tr);
      const ordered = childOrder(state);
      const idx = ordered.findIndex((e) => e.id === entry.id);
      insertConnRow(state, tr, idx);
      evictSurplusChildren(state);
    } else {
      const tr = state.childTrs.get(entry.id);
      if (tr) {
        writeConnCells(tr, entry);
        // If this conn's deep-expand panel is open, refresh it so a panel
        // opened early in the connection's lifecycle keeps up to date.
        refreshExpansion(tr, entry.id);
      }
    }
  } else {
    evictSurplusChildren(state);
  }
}

export function toggleSessionExpansion(sessionId) {
  const state = sessionMap.get(sessionId);
  if (!state || !state.sessionTr) return;
  if (state.expanded) {
    collapseSession(state);
  } else {
    expandSession(state);
  }
  refreshSessionRow(state);
}

function applyPrefsChange() {
  if (!tbody) return;
  // Reorder session rows in place.
  const sessions = Array.from(sessionMap.values())
    .filter((s) => s.sessionTr)
    .sort((a, b) => {
      const cmp = (a.firstTs || '').localeCompare(b.firstTs || '');
      return prefs.sessionSort === 'newest' ? -cmp : cmp;
    });
  for (const s of sessions) {
    tbody.appendChild(s.sessionTr);
    if (s.expanded && s.connsTr) tbody.insertBefore(s.connsTr, s.sessionTr.nextSibling);
    if (s.expanded && s.connsTbody) {
      const ordered = childOrder(s);
      for (const entry of ordered) {
        const tr = s.childTrs.get(entry.id);
        if (!tr) continue;
        const exp = tr.nextElementSibling;
        s.connsTbody.appendChild(tr);
        if (exp && exp.classList && exp.classList.contains('expansion')) {
          s.connsTbody.appendChild(exp);
        }
      }
    }
  }
  // Apply new caps.
  evictSurplusSessions();
  for (const s of sessionMap.values()) {
    evictSurplusChildren(s);
  }
}

export function bindSettingsUI() {
  const sSort = document.getElementById('pref-session-sort');
  const cSort = document.getElementById('pref-conn-sort');
  const maxS  = document.getElementById('pref-max-sessions');
  const maxC  = document.getElementById('pref-max-conns');
  if (!sSort || !cSort || !maxS || !maxC) return;

  sSort.value = prefs.sessionSort;
  cSort.value = prefs.connectionSort;
  maxS.value = String(prefs.maxSessions);
  maxC.value = String(prefs.maxConnectionsPerSession);

  sSort.addEventListener('change', () => setPrefs({ sessionSort: sSort.value }));
  cSort.addEventListener('change', () => setPrefs({ connectionSort: cSort.value }));
  maxS.addEventListener('change', () => {
    const v = Math.max(1, parseInt(maxS.value, 10) || DEFAULT_PREFS.maxSessions);
    setPrefs({ maxSessions: v });
  });
  maxC.addEventListener('change', () => {
    const v = Math.max(1, parseInt(maxC.value, 10) || DEFAULT_PREFS.maxConnectionsPerSession);
    setPrefs({ maxConnectionsPerSession: v });
  });
}
