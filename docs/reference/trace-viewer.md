# Trace viewer

The trace viewer renders a JSONL event trace in two views: a
three-panel timeline (actors, tools, state changes) and a linear chat
feed. It has no framework dependency and runs in any modern browser.

## Module layout

The viewer lives in `shared/trace-viewer/` at the repository root.
This directory is the canonical source; both the ensemble CLI and the
Stage web app consume from it.

```
shared/trace-viewer/
  viewer.js                  Core viewer. Accepts a DataSource.
  sources/
    local-jsonl.js           Reads a JSONL file via fetch and polls for updates.
    stage-polling.js         Polls GET /v1/runs/{id}/events against the Stage API.
```

## DataSource interface

A DataSource supplies events to the viewer. Any object with the
following methods satisfies the contract:

```js
interface DataSource {
  // Returns run metadata (url, scenario, etc.). Called once at startup.
  getRunMetadata(): Promise<object>

  // Returns all events seen so far (sinceSeq = -1) or only events
  // with sequence_number > sinceSeq.
  getEvents(sinceSeq: number): Promise<Event[]>

  // Returns true when the run has reached a terminal state and no
  // further events will arrive.
  isComplete(): boolean

  // Registers a callback invoked whenever new events arrive.
  onUpdate(callback: (events: Event[]) => void): void

  // Starts the data source (initiates polling, opens SSE, etc.).
  start(): Promise<void>

  // Stops background polling.
  stop(): void
}
```

## Local JSONL source

`LocalJsonlSource` reads a JSONL file via `fetch`. It polls every two
seconds while the run is in progress (detected by the absence of a
`grader:` system note in the trace) and stops once the run completes.
The ensemble CLI's embedded trace server configures this source
automatically; no options are needed.

```js
import { mountViewer } from './viewer.js';
import { LocalJsonlSource } from './sources/local-jsonl.js';

const source = new LocalJsonlSource('trace.jsonl');
mountViewer(source);
```

## Stage polling source

`StagePollingSource` polls `GET /v1/runs/{run_id}/events?since={seq}`
every two seconds while the run status is `running` or `queued`. When
the run transitions to a terminal state (`completed`, `failed`, or
`cancelled`), it performs one final poll to capture any trailing events
and then stops.

```js
import { mountViewer } from './viewer.js';
import { StagePollingSource } from './sources/stage-polling.js';

const source = new StagePollingSource({
  baseUrl: 'https://stage.ensemble.sh',
  runId: '019542a3-4e7b-7000-8e1d-3f9a1c2d5e6f',
  apiKey: 'stage_sk_...',  // omit for public projects
});
mountViewer(source);
```

## Integration with the Stage web app

The Stage web app (in the separate `ensemble-stage` repository) imports
the shared viewer using a git submodule or a pinned commit. The Stage
app configures `StagePollingSource` with the run id from the page URL
and the user's session cookie in place of the API key.

To update the viewer in the Stage repo, advance the submodule pointer
or update the pinned commit hash after reviewing the diff in
`shared/trace-viewer/`.

## Integration with the ensemble CLI

The ensemble CLI embeds `shared/trace-viewer/viewer.js`,
`shared/trace-viewer/sources/local-jsonl.js`, and
`shared/trace-viewer/sources/stage-polling.js` directly into the
binary via Rust `include_str!`. The embedded server serves them at
their canonical paths so the ES module imports in `site/viewer.js`
resolve correctly. To update the embedded viewer, edit the files in
`shared/trace-viewer/` and rebuild the CLI.
