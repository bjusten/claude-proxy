// Journal UI entry module.
//
// Orchestration:
//   1. Open SSE first; events buffer until the seed merges in.
//   2. fetch /__journal/entries in parallel.
//   3. When seed resolves: replay seed oldest-first into session-table,
//      then drain the buffer in arrival order.

import { initThemeToggle } from './theme.js';
import { openStream } from './sse.js';
import { initTelemetry, handleTelemetryEvent } from './telemetry.js';
import { applyStatusBarSeed } from './status-bar.js';
import { applyStateColors, renderStateLegend } from './state-colors.js';
import {
  initSessionTable,
  upsertConnection,
  bindSettingsUI,
} from './session-table.js';

const tbody = document.getElementById('rows');
const table = document.getElementById('activity');
const emptyEl = document.getElementById('empty');
const connEl = document.getElementById('conn');

let emptyHasBeenHidden = false;

function setConn(status) {
  if (!connEl) return;
  connEl.classList.remove('conn-live', 'conn-reconnecting');
  if (status === 'live') {
    connEl.classList.add('conn-live');
    connEl.textContent = '● live';
  } else {
    connEl.classList.add('conn-reconnecting');
    connEl.textContent = '● reconnecting';
  }
}

function ensureTableVisible() {
  if (emptyHasBeenHidden) return;
  if (emptyEl && emptyEl.parentNode) {
    emptyEl.parentNode.removeChild(emptyEl);
  }
  if (table) table.hidden = false;
  emptyHasBeenHidden = true;
}

function ingest(entry) {
  if (!entry || entry.type !== 'proxy_connection') return;
  upsertConnection(entry);
  ensureTableVisible();
}

let seedPending = true;
const buffer = [];

function handleEvent(name, payload) {
  if (seedPending) {
    buffer.push({ name, payload });
    return;
  }
  if (name !== 'journal_activity') {
    handleTelemetryEvent(name, payload);
    return;
  }
  ingest(payload);
}

async function fetchSeed() {
  let resp;
  try {
    resp = await fetch('/__journal/entries', {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });
  } catch (err) {
    console.warn('Seed fetch failed', err);
    return [];
  }
  if (resp.status === 401) {
    window.location.href = '/__ui/login';
    return [];
  }
  if (!resp.ok) {
    console.warn('Seed fetch non-ok', resp.status);
    return [];
  }
  try {
    return await resp.json();
  } catch (err) {
    console.warn('Seed parse failed', err);
    return [];
  }
}

function mergeSeed(seed) {
  applyStatusBarSeed(seed);
  for (const entry of seed) ingest(entry);
  while (buffer.length) {
    const { name, payload } = buffer.shift();
    if (name !== 'journal_activity') {
      handleTelemetryEvent(name, payload);
    } else {
      ingest(payload);
    }
  }
  seedPending = false;
}

function boot() {
  initThemeToggle();
  applyStateColors();
  renderStateLegend(document.getElementById('state-legend'));
  initSessionTable(tbody);
  bindSettingsUI();
  initTelemetry();
  setConn('reconnecting');
  const stream = openStream({
    url: '/__journal/stream?topic=journal_activity&topic=gpu_change&topic=system_change',
    onEvent: handleEvent,
    onStatus: setConn,
  });
  window.addEventListener('beforeunload', () => stream.close());
  fetchSeed().then((seed) => {
    mergeSeed(seed);
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
