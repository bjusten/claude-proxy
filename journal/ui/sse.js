// SSE wrapper with deterministic exponential-backoff reconnect.
//
// openStream({ url, onEvent, onStatus }) returns { close }.
//
//   onEvent(eventName, dataObj)  -- called per SSE event with parsed JSON.
//                                   Bad JSON is logged and skipped.
//   onStatus(status)             -- 'live' after the first event of a
//                                   (re)connect cycle; 'reconnecting'
//                                   synchronously on any error.
//
// Auth: a 401 on the very first connect surfaces as readyState=CLOSED.
// We redirect to /__ui/login instead of looping reconnects against a
// dead endpoint.
//
// Backoff schedule: 1s, 2s, 4s, 8s, 16s, 30s (cap). Resets to 1s on the
// first event delivered after a (re)connect.

const INITIAL_BACKOFF_MS = 1000;
const MAX_BACKOFF_MS = 30000;

export function openStream({ url, onEvent, onStatus }) {
  let source = null;
  let backoff = INITIAL_BACKOFF_MS;
  let retryTimer = null;
  let closed = false;
  let everConnected = false;

  function scheduleReconnect() {
    if (closed) return;
    if (retryTimer !== null) return;
    onStatus('reconnecting');
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, backoff);
    backoff = Math.min(backoff * 2, MAX_BACKOFF_MS);
  }

  function connect() {
    if (closed) return;
    try {
      source = new EventSource(url);
    } catch (err) {
      console.warn('EventSource construct failed', err);
      scheduleReconnect();
      return;
    }

    const TOPICS = ['journal_activity', 'gpu_change', 'system_change'];
    for (const topic of TOPICS) {
      source.addEventListener(topic, (ev) => {
        let payload;
        try {
          payload = JSON.parse(ev.data);
        } catch (err) {
          console.warn('Bad SSE payload', err, ev.data);
          return;
        }
        if (backoff !== INITIAL_BACKOFF_MS || !everConnected) {
          backoff = INITIAL_BACKOFF_MS;
          everConnected = true;
          onStatus('live');
        }
        onEvent(topic, payload);
      });
    }

    source.onerror = () => {
      const wasClosed = source && source.readyState === EventSource.CLOSED;
      try { source && source.close(); } catch (_) {}
      source = null;
      if (wasClosed && !everConnected) {
        // 401 on first connect surfaces as CLOSED. Redirect to login.
        window.location.href = '/__ui/login';
        return;
      }
      scheduleReconnect();
    };
  }

  function close() {
    closed = true;
    if (retryTimer !== null) {
      clearTimeout(retryTimer);
      retryTimer = null;
    }
    if (source) {
      try { source.close(); } catch (_) {}
      source = null;
    }
  }

  connect();
  return { close };
}
