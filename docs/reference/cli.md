# CLI

This page is the reference for the `ensemble` binary. Every
subcommand, every flag, with type, default, behavior, side
effects, and one example invocation. The binary itself lives at
`./target/debug/ensemble` after `cargo build -p ensemble-cli`; the
release build lands at `./target/release/ensemble`.

## ensemble init

```
ensemble init <name> [--path PATH] [--world EXISTING_WORLD]
```

Scaffolds a new project skeleton. Two modes: with no `--world`,
creates a fresh world (a rust crate, a python package, a
scenarios directory, a personas directory, a `world.toml`
manifest, and a smoke scenario). With `--world EXISTING_WORLD`,
creates just a scenarios package bound to an already-registered
world, skipping the world boilerplate.

Arguments:

- `<name>` (positional, required): the name of the new world or
  the scenario directory. Used as the world's registry name in the
  full-world form, and as the scenarios directory name in the
  bound form.
- `--path PATH`: where to create the project. Defaults to
  `./<name>`. The directory must not already exist; the command
  refuses to overwrite.
- `--world EXISTING_WORLD`: when set, scaffolds a scenarios
  directory bound to `EXISTING_WORLD` rather than creating a new
  world. The scaffolded scenario imports `EXISTING_WORLD` at
  module top so the world plugin registers itself.

Side effects: creates the directory tree and writes the files.
Prints the resulting root path to stdout.

Example:

```bash
ensemble init my_world
# scaffolds my_world/world/, my_world/my_world/, my_world/scenarios/, etc.

ensemble init smoke_only --world plank
# scaffolds smoke_only/scenarios/ pointing at the already-installed plank
```

## ensemble run

```
ensemble run <scenario> [--world W] [--manifest M] [--package-dir D]
```

Runs a registered scenario and writes the trace to
`./traces/<scenario>.jsonl`. The CLI shells to
`uv run python -m ensemble.cli_run` so error traces land in
tracebacks rather than opaque string-formatting failures.

Arguments:

- `<scenario>` (positional, required): the scenario name as
  registered with `@scenario` or in a `scenarios.toml` manifest.
- `--world W`: the world the scenario constructs. Resolves through
  the worlds registry at `~/.ensemble/worlds.toml`. Defaults to
  `"plank"` so the README's quick start works without flags;
  passing the scenario's declared world is the usual case.
- `--manifest M`: optional path to a `scenarios.toml` file the
  loader registers before lookup. Used to drive declarative
  scenarios from the CLI.
- `--package-dir D`: directory holding the `scenarios/` python
  package to import. Defaults to the registered world's directory
  when `--world` resolves; otherwise to `examples/plank`.

Side effects: writes `./traces/<safe_scenario_name>.jsonl`
(creating `./traces/` if missing); unlinks any prior file at the
same path so each run starts fresh. Prints a single JSON line on
stdout containing the scenario name, the grader scores, and the
trace path.

Example:

```bash
ensemble run plank.refund_storm --world plank
# {"scenario": "plank.refund_storm", "scores": {...}, "trace_path": "traces/plank_refund_storm.jsonl"}
```

## ensemble trace view

```
ensemble trace view <trace> [--port PORT] [--site SITE_DIR]
```

Serves the trace viewer with the supplied trace baked in. The
viewer itself ships embedded in the binary; the trace is held in
memory and served from `/trace.jsonl`.

Arguments:

- `<trace>` (positional, required): path to a JSONL trace file.
- `--port PORT`: TCP port to bind on `127.0.0.1`. Default `8765`.
- `--site SITE_DIR`: directory to serve static assets from. When
  unset, the embedded viewer (html, js, css) is served. When set,
  the directory's files take precedence over the embedded copy and
  the trace is also written to `<site>/trace.jsonl` so the viewer
  can be reloaded after edits to the html.

Side effects: binds the listener and serves until interrupted.
Writes `<site>/trace.jsonl` when `--site` is provided.

Example:

```bash
ensemble trace view traces/plank_refund_storm.jsonl
# serving embedded viewer on http://127.0.0.1:8765

ensemble trace view site/trace.jsonl --site site --port 8765
# serving ./site/ on http://127.0.0.1:8765 with the file as the live trace
```

## ensemble worlds

Manages the user-level registry at `~/.ensemble/worlds.toml`. The
`ENSEMBLE_HOME` env var overrides the location (used by tests).
All four subcommands shell to `uv run python -m ensemble.cli_worlds`.

### worlds list

```
ensemble worlds list
```

Prints one line per registered world: `name  path  git=<url>`. The
git column is omitted for worlds without a remote.

Example:

```bash
ensemble worlds list
# plank  /Users/jane/code/ensemble/examples/plank
```

### worlds add

```
ensemble worlds add <name> <path> [--git URL]
```

Registers a world by local path. The manifest at
`<path>/world.toml` is parsed eagerly; a typo in the manifest
fails at add time rather than at scenario-run time.

- `<name>`: the short name. Must match the `world.name` field in
  the manifest.
- `<path>`: the world's directory.
- `--git URL`: optional informational git URL. Recorded but not
  used for cloning today.

Side effects: rewrites `~/.ensemble/worlds.toml`.

Example:

```bash
ensemble worlds add plank examples/plank
# registered plank -> /Users/jane/code/ensemble/examples/plank
```

### worlds remove

```
ensemble worlds remove <name>
```

Removes a world from the registry. Returns exit code 1 if the
name was not present.

### worlds show

```
ensemble worlds show <name>
```

Prints the world's manifest details: path, python package, rust
crate, personas dir, default personas, default tools.

## ensemble mcp serve

```
ensemble mcp serve --world W
ensemble mcp serve --world W --scenario S --as-agent A
                   [--package-dir D] [--backend BACKEND]
```

Runs an MCP server over stdio that exposes a world's tools to a
connected MCP-aware client. Two modes:

- Tools-only mode (`--world` only): the server exposes the
  world's tools as MCP tools. A client can list and call them but
  there is no scenario context.
- Scenario-driving mode (`--world --scenario --as-agent`): the
  named scenario runs in a background thread with the named agent
  slot registered as an external proxy. The connected MCP client
  drives that slot: its tool calls land in the trace attributed
  to the slot, and the meta tools `inbox_recv` / `agent_say`
  plumb scenario messages back and forth.

Arguments:

- `--world W` (required): the world name, resolved through the
  worlds registry.
- `--scenario S`: scenario to run while the server is up. Requires
  `--as-agent`.
- `--as-agent A`: agent slot id the connected client drives. The
  scenario must spawn an agent with this id; the server times out
  after 5 seconds if no matching spawn happens.
- `--package-dir D`: directory holding the scenarios package to
  import. Defaults to the world's directory.
- `--backend BACKEND`: the LLM backend for the non-external actors
  in the scenario. Defaults to `mock`.

Side effects: holds stdio open until the connected client
disconnects; runs the scenario in a daemon thread when
`--scenario` is set.

Example:

```bash
ensemble mcp serve --world plank
# expose plank's tools to whatever stdio client connects

ensemble mcp serve --world plank \
  --scenario plank.refund_storm --as-agent rep1
# rep1 is now driven by the connected client
```

## ensemble train

```
ensemble train <persona.toml> [--backend modal|skypilot|local]
```

Hands a persona TOML to the `ensemble-train` pipeline. The CLI
shells to `uv run ensemble-train`, so the training extra (`torch`,
`trl`, `peft`, etc.) must be installed for anything beyond a dry
run.

Arguments:

- `<persona.toml>` (positional, required): path to the persona
  TOML.
- `--backend`: where to dispatch the training job. `modal` (the
  default), `skypilot`, or `local`. The Modal backend falls back
  to a dry-run when the `modal` package isn't installed; the
  SkyPilot backend writes the YAML and prints the dispatch
  command when `sky` isn't on PATH.

Side effects: depends on the backend. `local` runs the trainer
in-process and writes a LoRA adapter under
`checkpoints/<persona_name>/`. `modal` submits a remote job and
writes a `DRY_RUN.json` next to the checkpoint when running
without the SDK installed. `skypilot` writes a SkyPilot YAML and
either launches via `sky` or prints the manual command.

Example:

```bash
ensemble train examples/plank/personas/frustrated_power_user.toml --backend modal
```
