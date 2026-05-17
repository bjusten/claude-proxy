// Collapsible JSON tree component.
//
// renderNode(value, { key } = {}) returns an HTMLElement representing
// `value`. Each node owns its own chevron toggle and copy affordance.
// Objects and arrays render collapsed; children populate on first
// expansion so deeply-nested structures (Anthropic message arrays with
// tool calls) don't pay the full layout cost up front.

const LONG_STRING_LIMIT = 500;

export function renderNode(value, opts = {}) {
  const key = opts.key;
  const node = document.createElement('div');
  node.className = 'jtnode';
  if (Array.isArray(value)) {
    node.classList.add('jt-array');
    populateContainer(node, key, value, 'array', opts);
  } else if (value !== null && typeof value === 'object') {
    node.classList.add('jt-object');
    populateContainer(node, key, value, 'object', opts);
  } else {
    node.classList.add('jt-leaf');
    populateLeaf(node, key, value);
  }
  return node;
}

function populateContainer(node, key, value, kind, opts = {}) {
  const header = document.createElement('span');
  header.className = 'jt-header jt-clickable';

  const chev = document.createElement('button');
  chev.type = 'button';
  chev.className = 'jt-chevron';
  chev.setAttribute('aria-label', 'toggle');
  // '+' when collapsed, '−' (U+2212) when expanded — U+2212 matches the
  // '+' glyph width so the toggle stays aligned across states.
  chev.textContent = '+';
  header.appendChild(chev);

  if (key !== undefined && key !== null) {
    const k = document.createElement('span');
    k.className = 'jt-key';
    k.textContent = formatKey(key) + ':';
    header.appendChild(k);
  }

  const summary = document.createElement('span');
  summary.className = 'jt-summary';
  if (kind === 'array') {
    summary.textContent = `[${value.length} items]`;
  } else {
    summary.textContent = `{${Object.keys(value).length} keys}`;
  }
  header.appendChild(summary);

  header.appendChild(makeCopyButton(value));

  const children = document.createElement('div');
  children.className = 'jt-children';
  children.hidden = true;

  let expanded = opts.expanded || false;
  let populated = false;
  function toggle(force) {
    if (force === true || children.hidden) {
      if (!populated) {
        if (kind === 'array') {
          for (let i = 0; i < value.length; i++) {
            children.appendChild(renderNode(value[i], { key: i }));
          }
        } else {
          for (const k of Object.keys(value)) {
            children.appendChild(renderNode(value[k], { key: k }));
          }
        }
        populated = true;
      }
      children.hidden = false;
      chev.textContent = '−';
      node.classList.add('jt-expanded');
    } else {
      children.hidden = true;
      chev.textContent = '+';
      node.classList.remove('jt-expanded');
    }
  }
  // Auto-expand on first load if requested
  if (expanded) toggle(true);

  header.addEventListener('click', (e) => {
    // Copy button stops its own event; "more" button likewise.
    if (e.target.closest('.jt-copy')) return;
    toggle();
  });

  node.appendChild(header);
  node.appendChild(children);
}

function populateLeaf(node, key, value) {
  const header = document.createElement('span');
  header.className = 'jt-header';

  if (key !== undefined && key !== null) {
    const k = document.createElement('span');
    k.className = 'jt-key';
    k.textContent = formatKey(key) + ':';
    header.appendChild(k);
  }

  header.appendChild(renderPrimitive(value));
  header.appendChild(makeCopyButton(value));
  node.appendChild(header);
}

function formatKey(key) {
  // Numeric array indices render bare; string object keys render quoted.
  return typeof key === 'number' ? String(key) : JSON.stringify(key);
}

function renderPrimitive(value) {
  const span = document.createElement('span');
  span.className = 'jt-value';
  if (value === null) {
    span.classList.add('jt-null');
    span.textContent = 'null';
  } else if (typeof value === 'boolean') {
    span.classList.add('jt-bool');
    span.textContent = String(value);
  } else if (typeof value === 'number') {
    span.classList.add('jt-number');
    span.textContent = String(value);
  } else if (typeof value === 'string') {
    span.classList.add('jt-string');
    renderString(span, value);
  } else {
    span.classList.add('jt-other');
    span.textContent = String(value);
  }
  return span;
}

function renderString(container, value) {
  const Q = '"';
  if (value.length <= LONG_STRING_LIMIT) {
    container.textContent = Q + value + Q;
    return;
  }
  const head = document.createElement('span');
  head.className = 'jt-string-head';
  head.textContent = Q + value.slice(0, LONG_STRING_LIMIT);

  const rest = document.createElement('span');
  rest.className = 'jt-string-rest';
  rest.hidden = true;
  rest.textContent = value.slice(LONG_STRING_LIMIT);

  const closing = document.createElement('span');
  closing.className = 'jt-string-tail';
  closing.textContent = Q;

  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'jt-more';
  const remaining = value.length - LONG_STRING_LIMIT;
  const moreLabel = `… more (${remaining} chars)`;
  more.textContent = moreLabel;
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    if (rest.hidden) {
      rest.hidden = false;
      more.textContent = 'less';
    } else {
      rest.hidden = true;
      more.textContent = moreLabel;
    }
  });

  container.appendChild(head);
  container.appendChild(rest);
  container.appendChild(closing);
  container.appendChild(more);
}

function makeCopyButton(value) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'jt-copy';
  btn.title = 'copy JSON to clipboard';
  btn.setAttribute('aria-label', 'copy');
  btn.textContent = '⧉';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    const text = serialiseForCopy(value);
    try {
      await navigator.clipboard.writeText(text);
      flash(btn, '✓');
    } catch (err) {
      console.warn('clipboard write failed', err);
      flash(btn, '!');
    }
  });
  return btn;
}

function serialiseForCopy(value) {
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch (_) {
    return String(value);
  }
}

function flash(btn, marker) {
  const prev = btn.textContent;
  btn.textContent = marker;
  setTimeout(() => { btn.textContent = prev; }, 900);
}
