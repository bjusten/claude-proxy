// Single source of truth for connection-state colours.
//
// The proxy injects operator-configured colours as `window.__stateColors`
// (see journal/ui_server.py). This module is the *only* consumer: it
// publishes them as CSS custom properties on :root so the state dot
// (session-table), the proxy-connection timeline (expand) and the legend
// all render from one configurable set. Change the colours in
// journal.json `ui.state_colors` and every surface follows.
//
// Backend state names come from proxy.py (STATE_* constants). SUCCESS /
// FAILURE render as a check / cross; every other state is a filled dot.

// Canonical order = the connection lifecycle, oldest → terminal.
export const STATES = [
  { key: 'INIT',             label: 'connected',  glyph: '●' },
  { key: 'CLASSIFYING',      label: 'classifying', glyph: '●' },
  { key: 'QUEUED',           label: 'queued',     glyph: '●' },
  { key: 'ROUTING_REQUEST',  label: 'sending',    glyph: '●' },
  { key: 'ROUTING_RESPONSE', label: 'awaiting',   glyph: '●' },
  { key: 'SUCCESS',          label: 'success',    glyph: '✓' },
  { key: 'FAILURE',          label: 'failure',    glyph: '✗' },
];

const BY_KEY = new Map(STATES.map((s) => [s.key, s]));

// 'ROUTING_REQUEST' → '--state-routing-request'
export function stateCssVar(stateKey) {
  return `--state-${String(stateKey).toLowerCase().replace(/_/g, '-')}`;
}

// CSS colour expression for a state, falling back to the dim fg colour
// for unknown / missing states.
export function stateColor(stateKey) {
  if (!BY_KEY.has(stateKey)) return 'var(--fg)';
  return `var(${stateCssVar(stateKey)})`;
}

export function stateGlyph(stateKey) {
  const s = BY_KEY.get(stateKey);
  return s ? s.glyph : '●';
}

// Publish injected colours as :root custom properties. Safe to call with
// no injection present — the style.css fallbacks then apply.
export function applyStateColors() {
  const colors = (typeof window !== 'undefined' && window.__stateColors) || {};
  const root = document.documentElement;
  for (const { key } of STATES) {
    const v = colors[key];
    if (typeof v === 'string' && v) root.style.setProperty(stateCssVar(key), v);
  }
}

// Build the compact horizontal legend into `container` (idempotent).
export function renderStateLegend(container) {
  if (!container) return;
  container.textContent = '';
  container.classList.add('state-legend');
  for (const { key, label, glyph } of STATES) {
    const item = document.createElement('span');
    item.className = 'state-legend-item';
    const mark = document.createElement('span');
    mark.className = 'state-legend-mark';
    mark.textContent = glyph;
    mark.style.color = stateColor(key);
    const text = document.createElement('span');
    text.className = 'state-legend-text';
    text.textContent = label;
    item.append(mark, text);
    container.appendChild(item);
  }
}
