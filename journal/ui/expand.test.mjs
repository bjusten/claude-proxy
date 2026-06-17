// Unit tests for the pure exports of expand.js, run with Node's built-in
// test runner (no dependencies):  node --test journal/ui/
//
// Only DOM-free pure functions are covered here; the rendering helpers need a
// DOM and are exercised end-to-end via the Python UI-server tests.

import { test } from 'node:test';
import assert from 'node:assert/strict';

import { parseSSEStream, timelineModel } from './expand.js';

// --- parseSSEStream ---------------------------------------------------------

test('parseSSEStream assembles content and finish_reason', () => {
  const body = [
    'data: {"choices":[{"delta":{"role":"assistant"},"index":0}]}',
    '',
    'data: {"choices":[{"delta":{"content":"Hel"},"index":0}]}',
    '',
    'data: {"choices":[{"delta":{"content":"lo"},"index":0}]}',
    '',
    'data: {"choices":[{"delta":{"content":" world"},"index":0,"finish_reason":"stop"}]}',
    '',
    'data: [DONE]',
    '',
  ].join('\n');
  const r = parseSSEStream(body);
  assert.ok(r, 'recognized as a chat-completion stream');
  assert.equal(r.content, 'Hello world');
  assert.equal(r.finishReason, 'stop');
  assert.equal(r.frames, 4);
  assert.equal(r.reasoning, '');
  assert.equal(r.truncated, false);
});

test('parseSSEStream keeps reasoning_content separate from content', () => {
  const body = [
    'data: {"choices":[{"delta":{"reasoning_content":"Let me think. "}}]}',
    'data: {"choices":[{"delta":{"reasoning_content":"2+2=4."}}]}',
    'data: {"choices":[{"delta":{"content":"The answer "}}]}',
    'data: {"choices":[{"delta":{"content":"is 4."},"finish_reason":"stop"}]}',
    'data: [DONE]',
  ].join('\n');
  const r = parseSSEStream(body);
  assert.equal(r.content, 'The answer is 4.');
  assert.equal(r.reasoning, 'Let me think. 2+2=4.');
});

test('parseSSEStream flags a journal-truncated tail and keeps partial content', () => {
  const body = [
    'data: {"choices":[{"delta":{"content":"partial "}}]}',
    'data: {"choices":[{"delta":{"content":"ans',  // cut mid-frame by the journal cap
    ' [... truncated]',
  ].join('\n');
  const r = parseSSEStream(body);
  assert.ok(r);
  assert.equal(r.content, 'partial ');
  assert.equal(r.truncated, true);
});

test('parseSSEStream supports legacy completion `text` deltas', () => {
  const body = [
    'data: {"choices":[{"text":"foo","index":0}]}',
    'data: {"choices":[{"text":"bar","index":0,"finish_reason":"stop"}]}',
    'data: [DONE]',
  ].join('\n');
  const r = parseSSEStream(body);
  assert.equal(r.content, 'foobar');
});

test('parseSSEStream returns null for non-stream bodies', () => {
  // Plain (non-streaming) JSON response — must fall through to JSON rendering.
  const plainJson = JSON.stringify({ id: 'x', choices: [{ message: { content: 'hi' } }] }, null, 2);
  assert.equal(parseSSEStream(plainJson), null);
  // HTML error page.
  assert.equal(parseSSEStream('<html><body>error</body></html>'), null);
  // Empty / non-string.
  assert.equal(parseSSEStream(''), null);
  assert.equal(parseSSEStream(undefined), null);
  // SSE keep-alive / pings with no `choices` — not a chat-completion stream.
  assert.equal(parseSSEStream('data: {"type":"ping"}\n\ndata: {"hello":1}'), null);
});

// --- timelineModel ----------------------------------------------------------

const BASE = Date.parse('2026-06-17T00:00:00.000Z');
const iso = (ms) => new Date(BASE + ms).toISOString();

test('timelineModel returns null when no phase has fired', () => {
  assert.equal(timelineModel({}, { state: 'INIT' }), null);
  assert.equal(timelineModel(null, { state: 'INIT' }), null);
});

test('timelineModel builds closed segments for a finished connection', () => {
  const ts = {
    received: iso(0),
    classified: iso(100),
    routed: iso(300),
    response_started: iso(600),
    response_completed: iso(1600),
  };
  const m = timelineModel(ts, { state: 'SUCCESS' });
  assert.ok(m);
  assert.equal(m.liveSeg, null, 'terminal state appends no live segment');
  assert.equal(m.firstT, BASE);
  assert.equal(m.lastT, BASE + 1600);
  assert.deepEqual(
    m.closed.map((s) => [s.label, s.ms, s.state]),
    [
      ['classifying', 100, 'CLASSIFYING'],
      ['queued', 200, 'QUEUED'],
      ['request', 300, 'ROUTING_REQUEST'],
      ['response', 1000, 'ROUTING_RESPONSE'],
    ],
  );
});

test('timelineModel grows a live "request" segment while awaiting first byte', () => {
  // ROUTING_RESPONSE before response_started lands: blocked waiting on the
  // upstream's first byte, shown as the open "request" gap.
  const ts = { received: iso(0), classified: iso(100), routed: iso(300) };
  const m = timelineModel(ts, { state: 'ROUTING_RESPONSE' }, BASE + 800);
  assert.ok(m.liveSeg);
  assert.equal(m.liveSeg.label, 'request');
  assert.equal(m.liveSeg.state, 'ROUTING_REQUEST');
  assert.equal(m.liveSeg.ms, 500); // now(800) - routed(300)
  assert.equal(m.liveSeg.live, true);
});

test('timelineModel grows a live "response" segment once the body is streaming', () => {
  // ROUTING_RESPONSE after response_started: receiving the streamed body.
  const ts = {
    received: iso(0), classified: iso(100), routed: iso(300), response_started: iso(600),
  };
  const m = timelineModel(ts, { state: 'ROUTING_RESPONSE' }, BASE + 2000);
  assert.ok(m.liveSeg);
  assert.equal(m.liveSeg.label, 'response');
  assert.equal(m.liveSeg.state, 'ROUTING_RESPONSE');
  assert.equal(m.liveSeg.ms, 1400); // now(2000) - response_started(600)
});

test('timelineModel shows a live "classifying" segment with only `received`', () => {
  const m = timelineModel({ received: iso(0) }, { state: 'CLASSIFYING' }, BASE + 50);
  assert.deepEqual(m.closed, []);
  assert.equal(m.liveSeg.label, 'classifying');
  assert.equal(m.liveSeg.state, 'CLASSIFYING');
  assert.equal(m.liveSeg.ms, 50);
});
