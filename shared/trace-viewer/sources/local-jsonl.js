// LocalJsonlSource: reads events from a JSONL file via fetch.
//
// The source polls every two seconds while the run is still in
// progress, stopping once it sees a "grader" system note in the
// trace (which is always the last event a scenario emits).
//
// Usage:
//   const src = new LocalJsonlSource('trace.jsonl');
//   src.onUpdate((events) => render(events));
//   await src.start();

export class LocalJsonlSource {
  constructor(url) {
    this._url = url;
    this._events = [];
    this._complete = false;
    this._callbacks = [];
    this._pollTimer = null;
  }

  async getRunMetadata() {
    return { url: this._url };
  }

  async getEvents(sinceSeq = -1) {
    return sinceSeq < 0
      ? this._events
      : this._events.filter((e) => (e.tick || 0) > sinceSeq);
  }

  isComplete() {
    return this._complete;
  }

  onUpdate(callback) {
    this._callbacks.push(callback);
  }

  async start() {
    await this._poll();
    if (!this._complete) {
      this._pollTimer = setInterval(() => this._poll(), 2000);
    }
  }

  stop() {
    if (this._pollTimer) {
      clearInterval(this._pollTimer);
      this._pollTimer = null;
    }
  }

  async _poll() {
    try {
      const resp = await fetch(this._url, { cache: 'no-store' });
      if (!resp.ok) return;
      const text = await resp.text();
      const parsed = text
        .split('\n')
        .map((s) => s.trim())
        .filter(Boolean)
        .map((s) => { try { return JSON.parse(s); } catch { return null; } })
        .filter(Boolean);
      const before = this._events.length;
      this._events = parsed;
      this._complete = _isComplete(parsed);
      if (this._complete && this._pollTimer) {
        clearInterval(this._pollTimer);
        this._pollTimer = null;
      }
      if (this._events.length !== before) {
        for (const cb of this._callbacks) cb(this._events);
      }
    } catch (_) {
      // Ignore transient fetch errors so polling survives a momentary
      // disk-write race during an active run.
    }
  }
}

function _isComplete(events) {
  for (let i = events.length - 1; i >= 0; i--) {
    const p = events[i].payload;
    if (p && p.kind === 'system' && typeof p.note === 'string' && p.note.startsWith('grader:')) {
      return true;
    }
  }
  return false;
}
