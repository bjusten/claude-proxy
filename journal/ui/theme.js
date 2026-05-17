// Theme toggle: 3-state cycle (system → light → dark → system).
//
// Resolution priority on load:
//   1. localStorage "journal.ui.theme"  (user's persisted choice)
//   2. window.__themeDefault             (server config, injected in <head>)
//   3. "system"                          (fallback)
//
// "system" means no `data-theme` attribute on <html>, so the CSS
// `@media (prefers-color-scheme)` rules drive the colors. An explicit
// "light"/"dark" sets `data-theme`, which wins via selector specificity.

const STORAGE_KEY = 'journal.ui.theme';
const STATES = ['system', 'light', 'dark'];
const ICON = { system: '🌓', light: '☀️', dark: '🌙' };

function normalize(value) {
  return STATES.includes(value) ? value : null;
}

function readInitial() {
  const stored = normalize(localStorage.getItem(STORAGE_KEY));
  if (stored) return stored;
  const fromServer = normalize(window.__themeDefault);
  if (fromServer) return fromServer;
  return 'system';
}

function apply(state) {
  const root = document.documentElement;
  if (state === 'system') {
    root.removeAttribute('data-theme');
  } else {
    root.setAttribute('data-theme', state);
  }
}

function persist(state) {
  try {
    localStorage.setItem(STORAGE_KEY, state);
  } catch (err) {
    // Private-mode / quota: theme still applies for this session.
    console.warn('[theme] persist failed', err);
  }
}

export function initThemeToggle() {
  let state = readInitial();
  apply(state);

  const btn = document.getElementById('theme-toggle');
  if (!btn) return;

  function render() {
    btn.textContent = ICON[state];
    btn.title = `Theme: ${state} (click to change)`;
  }
  render();

  btn.addEventListener('click', () => {
    const next = STATES[(STATES.indexOf(state) + 1) % STATES.length];
    state = next;
    apply(state);
    persist(state);
    render();
  });
}
