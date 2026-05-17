// GPU + system telemetry panels and toggles.
//
// State:
//   - localStorage `journal-ui:show-gpu` and `journal-ui:show-system` persist
//     the toggle (default off).
//   - `body.show-gpu` / `body.show-system` mirror the toggle so CSS can
//     hide the panel.
//   - `latest.{gpu,system}` caches the most recent sample so toggling
//     on does not refetch when an SSE event has already landed.
//
// Public surface:
//   initTelemetry()                — wires checkboxes, applies persisted
//                                    state, seeds the panel when on.
//   handleTelemetryEvent(topic, p) — handler for `gpu_change` /
//                                    `system_change` SSE payloads.

const STORAGE_KEYS = {
  gpu: 'journal-ui:show-gpu',
  system: 'journal-ui:show-system',
};
const TOGGLE_IDS = { gpu: 'show-gpu', system: 'show-system' };
const PANEL_IDS  = { gpu: 'gpu-panel', system: 'system-panel' };
const BODY_CLASSES = { gpu: 'show-gpu', system: 'show-system' };

const SEED_URLS  = { gpu: '/__journal/gpu', system: '/__journal/system' };
const EMPTY_TEXT = {
  gpu: 'GPU telemetry not available',
  system: 'System telemetry not available',
};

const latest = { gpu: null, system: null };

const SERVER_DEFAULTS = {
  gpu: () => window.__showGpuDefault === true,
  system: () => window.__showSystemDefault === true,
};

// `enable_gpu` / `enable_system` from journal.json. When false the poller is
// off, so the toggle is hidden and the panel never polls or renders.
// Default true: an older server that doesn't inject the flag still works.
const SERVER_ENABLED = {
  gpu: () => window.__gpuEnabled !== false,
  system: () => window.__systemEnabled !== false,
};

function readToggle(kind) {
  try {
    const stored = localStorage.getItem(STORAGE_KEYS[kind]);
    if (stored === '1') return true;
    if (stored === '0') return false;
  } catch (_) { /* private mode, etc. — fall through to server default */ }
  // No persisted preference: honour the operator-configured default.
  return SERVER_DEFAULTS[kind]();
}

function writeToggle(kind, on) {
  try { localStorage.setItem(STORAGE_KEYS[kind], on ? '1' : '0'); }
  catch (_) { /* private mode, etc. */ }
}

function applyBodyClass(kind, on) {
  if (document.body) document.body.classList.toggle(BODY_CLASSES[kind], on);
}

function showPanel(kind, on) {
  const el = document.getElementById(PANEL_IDS[kind]);
  if (el) el.hidden = !on;
}

function field(label, value) {
  const row = document.createElement('div');
  row.className = 'telemetry-line';
  const k = document.createElement('span');
  k.className = 'telemetry-line-key';
  k.textContent = label;
  const v = document.createElement('span');
  v.className = 'telemetry-line-val';
  v.textContent = String(value);
  row.appendChild(k);
  row.appendChild(v);
  return row;
}

function formatDuration(seconds) {
  if (seconds === undefined || seconds === null) return '—';
  const s = Math.max(0, Math.round(seconds));
  const days = Math.floor(s / 86400);
  const hours = Math.floor((s % 86400) / 3600);
  const mins = Math.floor((s % 3600) / 60);
  const secs = s % 60;
  const parts = [];
  if (days) parts.push(`${days}d`);
  if (hours) parts.push(`${hours}h`);
  if (mins) parts.push(`${mins}m`);
  if (secs || !parts.length) parts.push(`${secs}s`);
  return parts.join(' ');
}

function formatBytes(bytes) {
  if (bytes === undefined || bytes === null) return '—';
  const b = Math.abs(bytes);
  if (b >= 1099511627776) return `${(b / 1099511627776).toFixed(1)} TB`;
  if (b >= 1073741824) return `${(b / 1073741824).toFixed(1)} GB`;
  if (b >= 1048576) return `${(b / 1048576).toFixed(1)} MB`;
  if (b >= 1024) return `${(b / 1024).toFixed(1)} KB`;
  return `${b} B`;
}

/** Create a horizontal flex row with the given children. */
function hrow(...children) {
  const el = document.createElement('div');
  el.className = 'hrow';
  for (const c of children) el.appendChild(c);
  return el;
}

/** Create a compact horizontal chip (label styled separately from value).
 *  `extraClass` (e.g. 'hrow-chip-end') tweaks placement within the row. */
function hfield(label, value, extraClass) {
  const el = document.createElement('span');
  el.className = extraClass ? `hrow-chip ${extraClass}` : 'hrow-chip';
  const k = document.createElement('span');
  k.className = 'hrow-chip-label';
  k.textContent = label;
  const v = document.createElement('span');
  v.className = 'hrow-chip-val';
  v.textContent = value;
  el.appendChild(k);
  el.appendChild(v);
  return el;
}

/** Capacity bucket → soft green / yellow / red. */
function meterClass(pct) {
  if (pct >= 85) return 'meter-red';
  if (pct >= 60) return 'meter-yellow';
  return 'meter-green';
}

/**
 * Horizontal capacity bar laid out as a 3-column grid: label · track · value.
 * `pct` drives the fill width + colour; the label and exact numbers sit in
 * their own columns so they stay aligned and readable across rows.
 */
function meter(label, pct, valueText) {
  const p = Math.max(0, Math.min(100, Number.isFinite(pct) ? pct : 0));
  const m = document.createElement('div');
  m.className = 'meter';
  const k = document.createElement('span');
  k.className = 'meter-label';
  k.textContent = label;
  const track = document.createElement('div');
  track.className = 'meter-track';
  const fill = document.createElement('div');
  fill.className = `meter-fill ${meterClass(p)}`;
  fill.style.width = `${p}%`;
  track.appendChild(fill);
  const v = document.createElement('span');
  v.className = 'meter-val';
  v.textContent = valueText;
  m.appendChild(k);
  m.appendChild(track);
  m.appendChild(v);
  return m;
}

function emptyEl(text) {
  const div = document.createElement('div');
  div.className = 'telemetry-empty';
  div.textContent = text;
  return div;
}

function renderGpu(sample) {
  if (!sample || !Array.isArray(sample.gpus) || sample.gpus.length === 0) {
    return [emptyEl(EMPTY_TEXT.gpu)];
  }
  return sample.gpus.map((g) => {
    const card = document.createElement('div');
    card.className = 'gpu-card';

    // Identity + scalar readouts that have no natural capacity.
    card.appendChild(hrow(
      hfield('index', g.index),
      hfield('name', g.name),
      hfield('temp', `${g.temperature_c} °C`, 'hrow-chip-end'),
    ));

    card.appendChild(meter('util', g.utilization_gpu, `${g.utilization_gpu}%`));

    const memUsed = g.memory_used_mib * 1048576;
    const memTotal = g.memory_total_mib * 1048576;
    const memPct = memTotal > 0 ? (memUsed / memTotal) * 100 : 0;
    card.appendChild(meter('memory', memPct,
      `${formatBytes(memUsed)} / ${formatBytes(memTotal)}`));

    const limit = g.power_limit_w;
    if (limit !== undefined && limit !== null && limit > 0) {
      const powPct = (g.power_draw_w / limit) * 100;
      card.appendChild(meter('power', powPct,
        `${g.power_draw_w} / ${limit} W`));
    } else {
      card.appendChild(hrow(hfield('power', `${g.power_draw_w} W`)));
    }
    return card;
  });
}

function renderSystem(sample) {
  if (!sample || sample.uptime_seconds === undefined) {
    return [emptyEl(EMPTY_TEXT.system)];
  }
  const card = document.createElement('div');
  card.className = 'system-card';

  // Row 1: uptime | load average
  const uptimeFields = [hfield('uptime', formatDuration(sample.uptime_seconds))];
  if (Array.isArray(sample.loadavg)) {
    uptimeFields.push(hfield('load', sample.loadavg.join(' / '), 'hrow-chip-end'));
  }
  card.appendChild(hrow(...uptimeFields));

  // Memory as a capacity bar (used / total).
  if (sample.memory) {
    const mt = sample.memory.total_bytes || 0;
    const mu = sample.memory.used_bytes || 0;
    const mPct = mt > 0 ? (mu / mt) * 100 : 0;
    card.appendChild(meter('memory', mPct,
      `${formatBytes(mu)} / ${formatBytes(mt)}`));
  }

  // Each disk gets its own capacity bar.
  if (Array.isArray(sample.disks)) {
    for (const d of sample.disks) {
      const dt = d.total_bytes || 0;
      const du = d.used_bytes || 0;
      const dPct = dt > 0 ? (du / dt) * 100 : 0;
      card.appendChild(meter(d.mount, dPct,
        `${formatBytes(du)} / ${formatBytes(dt)}`));
    }
  }

  return [card];
}

function render(kind, sample) {
  const body = document.getElementById(`${PANEL_IDS[kind]}-body`);
  if (!body) return;
  const children = kind === 'gpu' ? renderGpu(sample) : renderSystem(sample);
  body.replaceChildren(...children);
}

async function fetchSeed(kind) {
  try {
    const r = await fetch(SEED_URLS[kind], {
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' },
    });
    if (!r.ok) return null;
    const j = await r.json();
    if (!j || typeof j !== 'object' || Object.keys(j).length === 0) return null;
    return j;
  } catch (_) {
    return null;
  }
}

async function seedKind(kind) {
  const data = await fetchSeed(kind);
  if (data) latest[kind] = data;
  render(kind, latest[kind]);
}

function applyToggle(kind, on) {
  writeToggle(kind, on);
  applyBodyClass(kind, on);
  showPanel(kind, on);
  if (on) {
    if (latest[kind] !== null) {
      render(kind, latest[kind]);
    } else {
      seedKind(kind);
    }
  }
}

export function initTelemetry() {
  for (const kind of ['gpu', 'system']) {
    const cb = document.getElementById(TOGGLE_IDS[kind]);
    if (!SERVER_ENABLED[kind]()) {
      // Poller disabled server-side: drop the toggle and keep the panel off
      // so the client never polls or renders this kind.
      const label = cb && cb.closest('label');
      if (label) label.hidden = true;
      applyBodyClass(kind, false);
      showPanel(kind, false);
      continue;
    }
    const on = readToggle(kind);
    if (cb) {
      cb.checked = on;
      cb.addEventListener('change', () => applyToggle(kind, cb.checked));
    }
    applyToggle(kind, on);
  }
}

export function handleTelemetryEvent(topic, payload) {
  const kind = topic === 'gpu_change'
    ? 'gpu'
    : topic === 'system_change' ? 'system' : null;
  if (!kind) return;
  latest[kind] = payload;
  render(kind, payload);
}

