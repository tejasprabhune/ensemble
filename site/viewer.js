// Trace viewer entry point for the local site.
//
// Wires LocalJsonlSource (reads ./trace.jsonl from the same origin)
// into the shared viewer. The shared module lives at
// ../shared/trace-viewer/ and contains the DataSource-agnostic render
// logic and the two source implementations.

import { mountViewer } from '../shared/trace-viewer/viewer.js';
import { LocalJsonlSource } from '../shared/trace-viewer/sources/local-jsonl.js';

const source = new LocalJsonlSource('trace.jsonl');
mountViewer(source);
