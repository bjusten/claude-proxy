// Status bar: pinned proxy_launched entry (ts, pid, port) + live uptime tick.
//
// Public surface:
//   initStatusBar()           — renders the initial bar, starts the uptime tick.
//   applyStatusBarSeed(seed)  — called from app.js mergeSeed; finds the
//                                proxy_launched entry in the seed and renders.

let tickTimer = null;

function formatTs(v) {
  if (!v) return '';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  return d.toLocaleString();
}

function tick(uptimeEl, startTs) {
  const elapsed = Date.now() - new Date(startTs).getTime();
  uptimeEl.textContent = uptimeText(elapsed);
}

function uptimeText(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function renderStatusBar(entry) {
  const bar = document.getElementById('status-bar');
  if (!bar) return;
  bar.innerHTML = '';
  const parts = [
    `launched ${formatTs(entry.ts)}`,
    `pid ${entry.pid}`,
    `port ${entry.port}`,
  ];
  for (const text of parts) {
    const span = document.createElement('span');
    span.className = 'status-part';
    span.textContent = text;
    bar.appendChild(span);
  }
  const uptimeEl = document.createElement('span');
  uptimeEl.className = 'status-uptime';
  uptimeEl.textContent = uptimeText(Date.now() - new Date(entry.ts).getTime());
  bar.appendChild(uptimeEl);
  tickTimer = setInterval(() => tick(uptimeEl, entry.ts), 1000);
}

export function applyStatusBarSeed(seed) {
  if (tickTimer) return; // already rendered
  const entry = seed.find((e) => e && e.type === 'proxy_launched');
  if (entry) renderStatusBar(entry);
}
