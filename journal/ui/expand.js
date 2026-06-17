// Row-expansion panel for the Journal UI activity list.
//
// toggleExpansion(rowEl, entryId) toggles the panel beneath a row.
// First open fetches GET /__journal/entries/<id> and renders the deep
// entry — timestamps, routing decision, incoming / routed / response
// envelopes, optional error panel. Re-click collapses the panel.
//
// Bodies render as a collapsible JSON tree when they parse as JSON,
// otherwise as <pre> text (HTML error pages). A streaming response body —
// captured as a raw `data: {...}` SSE event stream — is reassembled into the
// finished message, with the raw stream kept behind a collapsed toggle.
// Headers render as a collapsible <details> key/value table.

import { renderNode } from './json-tree.js';
import { stateColor } from './state-colors.js';

function getColSpan(rowEl) {
  // Count columns from the connection row's own nested conn-table header.
  if (rowEl) {
    const table = rowEl.closest('table.conn-table');
    const htr = table && table.querySelector('thead tr');
    if (htr && htr.children.length) return htr.children.length;
  }
  return 12; // COLUMNS.length fallback
}

const PHASES = [
  'received',
  'classified',
  'routed',
  'response_started',
  'response_completed',
];

function el(tag, opts = {}, ...children) {
  const e = document.createElement(tag);
  if (opts.className) e.className = opts.className;
  if (opts.text != null) e.textContent = opts.text;
  if (opts.attrs) {
    for (const [k, v] of Object.entries(opts.attrs)) e.setAttribute(k, v);
  }
  for (const c of children) if (c) e.appendChild(c);
  return e;
}

// Classification chrome is hidden unless the proxy enabled it (flag
// injected into index.html's <head>). Kept local to avoid an import
// cycle with session-table, which owns the matching column gate.
function classificationEnabled() {
  return typeof window !== 'undefined' && window.__classificationEnabled === true;
}

// Soft green / yellow / red bucket for an HTTP status code (mirrors the
// conn-table helper; kept local to avoid an import cycle with session-table).
function statusClass(code) {
  const n = Number(code);
  if (!Number.isFinite(n) || n <= 0) return '';
  if (n >= 500) return 'status-5xx';
  if (n >= 400) return 'status-4xx';
  if (n >= 300) return 'status-3xx';
  if (n >= 200) return 'status-2xx';
  return '';
}

function formatRelative(receivedIso, isoTs) {
  if (!receivedIso || !isoTs) return '';
  const a = Date.parse(receivedIso);
  const b = Date.parse(isoTs);
  if (Number.isNaN(a) || Number.isNaN(b)) return '';
  const delta = b - a;
  if (delta === 0) return '+0ms';
  if (Math.abs(delta) < 1000) return `${delta >= 0 ? '+' : ''}${delta}ms`;
  return `${delta >= 0 ? '+' : ''}${(delta / 1000).toFixed(3)}s`;
}

function formatGap(ms) {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(3)}s`;
}

// Human duration: 135.06ms / 1.23s / 3m12s. Returns null for non-numbers.
function formatDuration(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n)) return null;
  if (n < 1000) return `${n.toFixed(2)}ms`;
  const totalSec = n / 1000;
  if (totalSec < 60) return `${totalSec.toFixed(2)}s`;
  const m = Math.floor(totalSec / 60);
  const s = Math.round(totalSec - m * 60);
  return `${m}m${String(s).padStart(2, '0')}s`;
}

// Each timeline segment uses the exact connection-state colour that is
// active while that phase gap elapses, so the bar reads in the same
// palette as the state dot and legend. Keyed by the segment's *end*
// phase: the gap represents the work that finishes at that phase. Keying
// by the end (not the start) phase keeps the bar correct when a phase is
// skipped — e.g. with classification disabled the `classified` timestamp
// is never written, so no segment ends there and the `received -> routed`
// gap is correctly shown as 'queued' rather than a phantom 'classifying'.
// `label` is the short name shown in the segment legend.
const SEG_BY_END_PHASE = {
  classified:         { label: 'classifying', state: 'CLASSIFYING' },
  routed:             { label: 'queued',      state: 'QUEUED' },
  response_started:   { label: 'request',     state: 'ROUTING_REQUEST' },
  response_completed: { label: 'response',    state: 'ROUTING_RESPONSE' },
};

// SUCCESS / FAILURE are closed: the final timestamp is recorded, so no
// live segment is appended.
function isTerminalState(state) {
  return state === 'SUCCESS' || state === 'FAILURE';
}

// The trailing, still-open segment for an in-flight connection ends at the
// next timestamp the backend will write. We derive that *next end phase*
// from the current state plus which timestamps already exist, then reuse
// SEG_BY_END_PHASE — so the growing segment is laballed and coloured
// exactly as it will be once it closes (no relabel/recolour snap when the
// timestamp lands).
//
// ROUTING_RESPONSE is special: the proxy holds that one state across two
// distinct gaps — the wait for the first response byte (ends at
// `response_started`, shown as 'request': connection open + send + the
// model's time-to-first-token) and the body transfer (ends at
// `response_completed`, shown as 'response': the model streaming its
// answer). They are told apart by whether `response_started` has arrived.
// For a non-streaming upstream the first byte only comes once generation
// is complete, so the whole wait falls into the 'request' gap — a
// measurement limitation of buffered responses, not a labelling bug.
function liveEndPhase(state, recorded) {
  switch (state) {
    case 'CLASSIFYING':     return 'classified';
    case 'QUEUED':          return 'routed';
    case 'ROUTING_REQUEST': return 'routed';
    case 'ROUTING_RESPONSE':
      return recorded.has('response_started') ? 'response_completed' : 'response_started';
    default:                return null; // INIT / unknown: no recorded end phase yet
  }
}

// Pure timeline model — exported for unit testing. Returns null when no
// phase has fired yet. `closed` are the recorded phase-to-phase gaps;
// `liveSeg` (or null) is the open gap from the last recorded timestamp to
// `nowMs` for the still-running state. `nowMs` is injectable so tests are
// deterministic; production passes Date.now().
export function timelineModel(timestamps, entry, nowMs) {
  const points = [];
  const recorded = new Set();
  for (const phase of PHASES) {
    const ts = timestamps && timestamps[phase];
    if (!ts) continue;
    const t = Date.parse(ts);
    if (Number.isNaN(t)) continue;
    recorded.add(phase);
    points.push({ phase, ts, t });
  }
  if (points.length === 0) return null;

  const closed = [];
  for (let i = 0; i < points.length - 1; i++) {
    const def = SEG_BY_END_PHASE[points[i + 1].phase]
      || { label: points[i + 1].phase, state: 'INIT' };
    closed.push({ label: def.label, ms: points[i + 1].t - points[i].t, state: def.state });
  }

  const firstT = points[0].t;
  const lastT = points[points.length - 1].t;
  const st = entry && entry.state;
  const endPhase = (st && !isTerminalState(st)) ? liveEndPhase(st, recorded) : null;
  const liveDef = endPhase ? SEG_BY_END_PHASE[endPhase] : null;
  let liveSeg = null;
  if (liveDef) {
    const now = typeof nowMs === 'number' ? nowMs : Date.now();
    liveSeg = { label: liveDef.label, ms: Math.max(0, now - lastT), state: liveDef.state, live: true };
  }

  return {
    firstPhase: points[0].phase,
    firstTs: points[0].ts,
    firstT,
    lastT,
    closed,
    liveSeg,
  };
}

// Rebuild a timeline wrapper's bar + legend from its stashed entry. Called
// on first render and once a second by the ticker so the live segment
// grows in place. Returns true while the connection is still in flight
// (so the ticker keeps it registered).
function layoutTimeline(wrap) {
  const { timestamps, entry } = wrap._tl || {};
  const model = timelineModel(timestamps, entry);
  while (wrap.firstChild) wrap.removeChild(wrap.firstChild);
  if (!model) {
    wrap.appendChild(el('div', { className: 'exp-empty', text: '<no timestamps>' }));
    return false;
  }

  const segs = model.liveSeg ? model.closed.concat(model.liveSeg) : model.closed;
  const end = model.liveSeg ? model.lastT + model.liveSeg.ms : model.lastT;
  const total = end - model.firstT;

  const bar = el('div', { className: 'exp-ts-bar' });
  if (segs.length === 0) {
    // Single phase only — render a full-width INIT marker bar.
    const only = el('div', { className: 'exp-ts-seg exp-ts-seg-only' });
    only.style.background = stateColor('INIT');
    bar.appendChild(only);
  } else {
    for (const s of segs) {
      const pct = total > 0 ? (s.ms / total) * 100 : 100 / segs.length;
      const seg = el('div', { className: 'exp-ts-seg' + (s.live ? ' exp-ts-seg-live' : '') });
      seg.style.width = `${pct}%`;
      seg.style.backgroundColor = stateColor(s.state);
      seg.title = `${s.label}: ${formatGap(s.ms)}${s.live ? '…' : ''}`;
      bar.appendChild(seg);
    }
  }
  wrap.appendChild(bar);

  const legend = el('div', { className: 'exp-ts-legend' });
  if (segs.length === 0) {
    const sw = el('span', { className: 'exp-ts-swatch' });
    sw.style.background = stateColor('INIT');
    legend.appendChild(el('span', { className: 'exp-ts-legend-item' }, sw,
      el('span', { className: 'exp-ts-legend-text',
        text: `${model.firstPhase} @ ${model.firstTs}` }),
    ));
  } else {
    for (const s of segs) {
      const sw = el('span', { className: 'exp-ts-swatch' });
      sw.style.background = stateColor(s.state);
      legend.appendChild(el('span', { className: 'exp-ts-legend-item' }, sw,
        el('span', { className: 'exp-ts-legend-text', text: s.label }),
        el('span', { className: 'exp-ts-legend-dur',
          text: formatGap(s.ms) + (s.live ? '…' : '') }),
      ));
    }
    legend.appendChild(el('span', { className: 'exp-ts-legend-total',
      text: `total ${formatGap(total)}${model.liveSeg ? '…' : ''}` }));
  }
  wrap.appendChild(legend);
  return !!model.liveSeg;
}

// In-flight timelines re-layout once a second so the open segment visibly
// grows between SSE updates — the connection sits silent in the
// sending/awaiting stage while the upstream model works, mirroring the
// duration column's live ticker in session-table.js.
const liveTimelines = new Set();
let timelineTicker = null;

function ensureTimelineTicker() {
  if (timelineTicker !== null || typeof setInterval !== 'function') return;
  timelineTicker = setInterval(tickTimelines, 1000);
}

function tickTimelines() {
  for (const wrap of liveTimelines) {
    // Dropped from the DOM (collapsed / replaced by a refresh) — stop ticking.
    if (!wrap.isConnected) {
      liveTimelines.delete(wrap);
      continue;
    }
    if (!layoutTimeline(wrap)) liveTimelines.delete(wrap);
  }
}

function renderTimestamps(timestamps, entry) {
  const wrap = el('div', { className: 'exp-ts-timeline' });
  wrap._tl = { timestamps, entry };
  const live = layoutTimeline(wrap);
  if (live) {
    liveTimelines.add(wrap);
    ensureTimelineTicker();
  }
  return wrap;
}

function renderRouting(routed) {
  if (!routed) return el('div', { className: 'exp-empty', text: '<not routed>' });
  const dl = el('dl', { className: 'exp-kv' });
  function pair(k, v) {
    if (v === undefined || v === null) return;
    dl.appendChild(el('dt', { text: k }));
    dl.appendChild(el('dd', { text: typeof v === 'string' ? v : JSON.stringify(v) }));
  }
  pair('destination', routed.url);
  pair('original_model', routed.original_model);
  pair('new_model', routed.new_model);
  const r = routed.routing || {};
  pair('mode', r.mode);
  if (classificationEnabled()) {
    if (r.classified !== undefined) pair('classified', r.classified);
    if (r.scores !== undefined) pair('scores', r.scores);
  }
  if (r.entry_idx !== undefined) pair('entry_idx', r.entry_idx);
  return dl;
}

function renderClassification(classification) {
  if (!classification) return el('div', { className: 'exp-empty', text: '<no classification>' });
  const dl = el('dl', { className: 'exp-kv' });
  function pair(k, v) {
    if (v === undefined || v === null) return;
    dl.appendChild(el('dt', { text: k }));
    dl.appendChild(el('dd', { text: typeof v === 'string' ? v : JSON.stringify(v) }));
  }
  // The LLM emits its own word ('research'); the class map then routes it
  // to a destination class ('REASONING'). Show both so the mapping is
  // visible. `raw` is absent on the heuristic path — pair() skips nulls.
  pair('ai classification', classification.raw);
  pair('routed classification', classification.classified);
  if (classification.scores) {
    pair('duration', formatDuration(classification.scores.duration_ms));
    if (classification.scores.fallback !== undefined) pair('fallback', classification.scores.fallback);
  }
  return dl;
}

function renderHeadersTable(headers) {
  if (!headers || Object.keys(headers).length === 0) {
    return el('div', { className: 'exp-empty', text: '<no headers>' });
  }
  const tbody = el('tbody');
  for (const [k, v] of Object.entries(headers)) {
    tbody.appendChild(el('tr', {},
      el('td', { className: 'exp-hdr-key', text: k }),
      el('td', { className: 'exp-hdr-val', text: String(v) }),
    ));
  }
  return el('table', { className: 'exp-hdr-table' }, tbody);
}

function renderBody(body, { expanded = false } = {}) {
  if (body === undefined || body === null || body === '') {
    return el('div', { className: 'exp-empty', text: '<empty>' });
  }
  // Already-parsed object/array — render as tree directly.
  if (typeof body === 'object') {
    const wrap = el('div', { className: 'exp-body-tree' });
    wrap.appendChild(renderNode(body, { expanded }));
    return wrap;
  }
  const str = String(body);
  try {
    const value = JSON.parse(str);
    const wrap = el('div', { className: 'exp-body-tree' });
    wrap.appendChild(renderNode(value, { expanded }));
    return wrap;
  } catch (_) {
    return el('pre', { className: 'exp-pre', text: str });
  }
}

// Reassemble an OpenAI-style chat-completion SSE stream into the finished
// message. Streaming responses are journaled as the raw event stream — one
// `data: {...}` frame per token — which renders as an unreadable wall of
// chunks. This concatenates the per-token deltas back into the final answer.
// Returns null when *str* isn't a recognizable chat-completion event stream,
// so the caller falls back to the normal JSON-tree / <pre> rendering.
//
// Exported for unit testing.
export function parseSSEStream(str) {
  if (typeof str !== 'string' || str.indexOf('data:') === -1) return null;

  let content = '';
  let reasoning = '';
  let finishReason = null;
  let frames = 0;     // `data:` frames that parsed as JSON
  let badFrames = 0;  // frames that failed to parse (e.g. a truncated tail)
  let sawChoices = false;

  for (const line of str.split(/\r?\n/)) {
    const m = /^data:\s?(.*)$/.exec(line);
    if (!m) continue;
    const payload = m[1].trim();
    if (payload === '' || payload === '[DONE]') continue;
    let obj;
    try { obj = JSON.parse(payload); }
    catch (_) { badFrames++; continue; }
    frames++;
    const choices = Array.isArray(obj.choices) ? obj.choices : [];
    if (choices.length) sawChoices = true;
    for (const ch of choices) {
      const delta = ch.delta || {};
      if (typeof delta.content === 'string') content += delta.content;
      if (typeof delta.reasoning_content === 'string') reasoning += delta.reasoning_content;
      if (typeof ch.text === 'string') content += ch.text; // legacy completions
      if (ch.finish_reason) finishReason = ch.finish_reason;
    }
  }

  // Only claim the body when it actually looked like a chat-completion
  // stream — at least one frame carrying a `choices` array.
  if (!sawChoices) return null;

  const truncated = badFrames > 0 || /\[\.\.\. truncated\]/.test(str);
  return { content, reasoning, finishReason, frames, truncated };
}

// Render a reassembled stream: the final answer up front, with reasoning and
// the original raw event stream tucked into collapsed toggles so the default
// view is just the readable message.
function renderAssembledStream(sse, rawStr) {
  const wrap = el('div', { className: 'exp-assembled' });

  if (sse.content) {
    wrap.appendChild(el('pre', { className: 'exp-pre exp-assembled-content', text: sse.content }));
  } else {
    wrap.appendChild(el('div', { className: 'exp-empty', text: '<no assistant content>' }));
  }

  // Reasoning models (e.g. Qwen) stream a separate reasoning_content track.
  if (sse.reasoning) {
    const d = el('details', { className: 'exp-assembled-reasoning' });
    d.appendChild(el('summary', { text: 'reasoning' }));
    d.appendChild(el('pre', { className: 'exp-pre', text: sse.reasoning }));
    wrap.appendChild(d);
  }

  const bits = [`assembled from ${sse.frames} streamed event${sse.frames === 1 ? '' : 's'}`];
  if (sse.finishReason) bits.push(`finish: ${sse.finishReason}`);
  if (sse.truncated) bits.push('stream truncated in journal');
  wrap.appendChild(el('div', { className: 'exp-assembled-meta', text: bits.join(' · ') }));

  // The raw event stream stays available — collapsed — for debugging.
  const raw = el('details', { className: 'exp-assembled-raw' });
  raw.appendChild(el('summary', { text: 'raw stream' }));
  raw.appendChild(el('pre', { className: 'exp-pre', text: rawStr }));
  wrap.appendChild(raw);

  return wrap;
}

// Response bodies that are SSE streams reassemble into the final message;
// everything else (plain JSON, HTML error pages) renders normally.
function renderResponseBody(env, opts) {
  if (env && typeof env.body === 'string') {
    const sse = parseSSEStream(env.body);
    if (sse) return renderAssembledStream(sse, env.body);
  }
  return renderBody(env ? env.body : undefined, opts);
}

function headersDetails(headers) {
  const d = el('details', { className: 'exp-headers' });
  d.appendChild(el('summary', { text: 'headers' }));
  d.appendChild(renderHeadersTable(headers));
  return d;
}

function renderIncomingOrRouted(label, env) {
  if (!env) return null;
  const section = el('section', { className: `exp-section exp-env exp-env-${label}` });
  section.appendChild(el('h3', { text: label }));
  section.appendChild(headersDetails(env.headers));
  section.appendChild(el('div', { className: 'exp-body-label', text: 'body' }));
  section.appendChild(renderBody(env.body));
  return section;
}

function renderRequest(req) {
  if (!req) return null;
  const section = el('section', { className: 'exp-section exp-env exp-env-request' });
  section.appendChild(el('h3', { text: 'request' }));
  // Metadata: url and model as a key-value list.
  const meta = el('dl', { className: 'exp-kv' });
  if (req.url) {
    meta.appendChild(el('dt', { text: 'url' }));
    meta.appendChild(el('dd', { text: req.url }));
  }
  if (req.model) {
    meta.appendChild(el('dt', { text: 'model' }));
    meta.appendChild(el('dd', { text: req.model }));
  }
  section.appendChild(meta);
  section.appendChild(headersDetails(req.headers));
  section.appendChild(el('div', { className: 'exp-body-label', text: 'body' }));
  section.appendChild(renderBody(req.body));
  return section;
}

function renderResponse(env, { autoExpandBody = true } = {}) {
  const section = el('section', { className: 'exp-section exp-env exp-env-response' });
  section.appendChild(el('h3', { text: 'response' }));
  if (!env) {
    section.appendChild(el('div', { className: 'exp-empty', text: '<no response yet>' }));
    return section;
  }
  const meta = el('div', { className: 'exp-response-meta' });
  if (env.status !== undefined && env.status !== null) {
    const cls = statusClass(env.status);
    meta.appendChild(el('span', {
      className: `exp-response-status status-pill${cls ? ' ' + cls : ''}`,
      text: `status ${env.status}`,
    }));
  }
  if (env.bytes !== undefined && env.bytes !== null) {
    meta.appendChild(el('span', { className: 'exp-response-bytes', text: `${env.bytes} bytes` }));
  }
  section.appendChild(meta);
  section.appendChild(headersDetails(env.headers));
  section.appendChild(el('div', { className: 'exp-body-label', text: 'body' }));
  // Render response body expanded by default so the full raw JSON is
  // immediately visible without having to click to expand the tree.
  // Exception: classification-request entries keep the tree collapsed —
  // the response is a one-word category, not payload worth pre-expanding.
  // Streaming SSE bodies are reassembled into the finished message instead
  // of dumping the per-token event stream (see renderResponseBody).
  section.appendChild(renderResponseBody(env, { expanded: autoExpandBody }));
  return section;
}

function renderError(err) {
  if (!err) return null;
  const section = el('section', { className: 'exp-section exp-error-panel' });
  section.appendChild(el('h3', { text: 'error' }));
  if (err.kind) {
    section.appendChild(el('div', { className: 'exp-error-kind', text: `kind: ${err.kind}` }));
  }
  if (err.message) {
    section.appendChild(el('div', { className: 'exp-error-message', text: err.message }));
  }
  return section;
}

function renderPanel(entry) {
  const wrap = el('div', { className: 'exp-wrap' });

  const tsSection = el('section', { className: 'exp-section exp-timestamps' });
  tsSection.appendChild(el('h3', { text: 'timestamps' }));
  tsSection.appendChild(renderTimestamps(entry.timestamps, entry));
  wrap.appendChild(tsSection);

  if (entry.classification) {
    const clsSection = el('section', { className: 'exp-section exp-classification' });
    clsSection.appendChild(el('h3', { text: 'classification' }));
    clsSection.appendChild(renderClassification(entry.classification));
    wrap.appendChild(clsSection);
  } else if (entry.routed) {
    const routingSection = el('section', { className: 'exp-section exp-routing' });
    routingSection.appendChild(el('h3', { text: 'routing' }));
    routingSection.appendChild(renderRouting(entry.routed));
    wrap.appendChild(routingSection);
  }

  const incomingSection = renderIncomingOrRouted('incoming', entry.incoming);
  if (incomingSection) wrap.appendChild(incomingSection);
  const reqSection = renderRequest(entry.request);
  if (reqSection) wrap.appendChild(reqSection);
  const routedSection = renderIncomingOrRouted('routed', entry.routed);
  if (routedSection) wrap.appendChild(routedSection);
  wrap.appendChild(renderResponse(entry.response, { autoExpandBody: !entry.classification }));

  const errSection = renderError(entry.error);
  if (errSection) wrap.appendChild(errSection);

  return wrap;
}

function makeExpansionRow(entryId, content, rowEl) {
  const tr = el('tr', { className: 'expansion', attrs: { 'data-expansion-for': entryId } });
  const td = document.createElement('td');
  td.colSpan = getColSpan(rowEl);
  td.appendChild(content);
  tr.appendChild(td);
  return tr;
}

function existingExpansionFor(rowEl, entryId) {
  const next = rowEl.nextElementSibling;
  if (next
      && next.classList.contains('expansion')
      && next.dataset.expansionFor === entryId) {
    return next;
  }
  return null;
}

async function fetchDeep(entryId) {
  let resp;
  try {
    resp = await fetch(`/__journal/entries/${encodeURIComponent(entryId)}`, {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });
  } catch (err) {
    return { error: `fetch failed: ${err}` };
  }
  if (resp.status === 401) {
    window.location.href = '/__ui/login';
    return { error: 'auth' };
  }
  if (!resp.ok) {
    return { error: `HTTP ${resp.status}` };
  }
  try {
    return { entry: await resp.json() };
  } catch (err) {
    return { error: `parse failed: ${err}` };
  }
}

export async function toggleExpansion(rowEl, entryId) {
  if (!rowEl || !entryId) return;

  const existing = existingExpansionFor(rowEl, entryId);
  if (existing) {
    existing.remove();
    return;
  }

  // Insert a loading row so re-clicks during fetch are absorbed.
  const loading = makeExpansionRow(
    entryId,
    el('div', { className: 'exp-loading', text: 'loading…' }),
    rowEl,
  );
  rowEl.parentNode.insertBefore(loading, rowEl.nextElementSibling);

  const { entry, error } = await fetchDeep(entryId);

  // User collapsed before fetch returned.
  if (!loading.isConnected) return;

  const content = error
    ? el('div', { className: 'exp-fetch-error', text: error })
    : renderPanel(entry);

  const fresh = makeExpansionRow(entryId, content, rowEl);
  loading.replaceWith(fresh);
}

// Re-render an already-open deep-expand panel in place. No-op if the row
// isn't expanded. Called when a connection updates while expanded so a
// panel opened early in the connection's lifecycle doesn't go stale.
export async function refreshExpansion(rowEl, entryId) {
  if (!rowEl || !entryId) return;
  const existing = existingExpansionFor(rowEl, entryId);
  if (!existing) return;

  const { entry, error } = await fetchDeep(entryId);

  // Collapsed (or replaced) while the fetch was in flight.
  if (!existing.isConnected) return;

  const content = error
    ? el('div', { className: 'exp-fetch-error', text: error })
    : renderPanel(entry);
  existing.replaceWith(makeExpansionRow(entryId, content, rowEl));
}
