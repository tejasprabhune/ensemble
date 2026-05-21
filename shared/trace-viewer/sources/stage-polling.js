// StagePollingSource: reads events from the Stage HTTP API.
//
// Polls GET /v1/runs/{run_id}/events?since={last_seq} every two
// seconds while the run status is "running" or "queued". Stops
// polling once the run reaches a terminal state (completed, failed,
// cancelled).
//
// Usage:
//   const src = new StagePollingSource({
//     baseUrl: 'https://stage.ensemble.sh',
//     runId: '019542a3-...',
//     apiKey: 'stage_sk_...',   // optional; omit for public projects
//   });
//   src.onUpdate((events) => render(events));
//   await src.start();

export class StagePollingSource {
  constructor({ baseUrl, runId, apiKey }) {
    this._baseUrl = baseUrl.replace(/\/$/, '');
    this._runId = runId;
    this._apiKey = apiKey || null;
    this._events = [];
    this._lastSeq = -1;
    this._complete = false;
    this._status = 'queued';
    this._callbacks = [];
    this._pollTimer = null;
  }

  async getRunMetadata() {
    const data = await this._fetch(`/v1/runs/${this._runId}`);
    return data;
  }

  async getEvents(sinceSeq = -1) {
    return sinceSeq < 0
      ? this._events
      : this._events.filter((e) => (e.sequence_number || 0) > sinceSeq);
  }

  isComplete() {
    return this._complete;
  }

  onUpdate(callback) {
    this._callbacks.push(callback);
  }

  async start() {
    try {
      const meta = await this.getRunMetadata();
      this._status = meta.status || 'queued';
      this._complete = _isTerminal(this._status);
    } catch (_) {}

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
      const newEvents = await this._fetch(
        `/v1/runs/${this._runId}/events?since=${this._lastSeq}`
      );
      if (!Array.isArray(newEvents) || newEvents.length === 0) {
        if (!this._complete) await this._checkStatus();
        return;
      }
      const before = this._events.length;
      for (const e of newEvents) {
        this._events.push(e);
        if ((e.sequence_number || 0) > this._lastSeq) {
          this._lastSeq = e.sequence_number;
        }
      }
      for (const cb of this._callbacks) cb(this._events);
      await this._checkStatus();
    } catch (_) {}
  }

  async _checkStatus() {
    try {
      const meta = await this._fetch(`/v1/runs/${this._runId}`);
      this._status = meta.status || this._status;
      if (_isTerminal(this._status) && !this._complete) {
        this._complete = true;
        if (this._pollTimer) {
          clearInterval(this._pollTimer);
          this._pollTimer = null;
        }
        // One final poll to capture any events emitted between the
        // last poll and the terminal status transition.
        await this._poll();
      }
    } catch (_) {}
  }

  async _fetch(path) {
    const headers = {};
    if (this._apiKey) headers['Authorization'] = `Bearer ${this._apiKey}`;
    const resp = await fetch(this._baseUrl + path, { headers });
    if (!resp.ok) throw new Error(`Stage API ${path}: HTTP ${resp.status}`);
    return resp.json();
  }
}

function _isTerminal(status) {
  return status === 'completed' || status === 'failed' || status === 'cancelled';
}
