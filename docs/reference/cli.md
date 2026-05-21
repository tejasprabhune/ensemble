# CLI

This page is the reference for the `ensemble` binary. Every
subcommand, every flag, with type, default, behavior, side
effects, and one example invocation. The binary itself lives at
`./target/debug/ensemble` after `cargo build -p ensemble-cli`; the
release build lands at `./target/release/ensemble`.

## ensemble init

```
ensemble init <name> [--path PATH] [--world EXISTING_WORLD] [--with-rust]
```

Scaffolds a new project skeleton. Three shapes:

- Default (`ensemble init <name>`): a pure-Python world. One module
  file with an example `@tool`, a `world.toml`, a runnable smoke
  scenario, and a README. No Rust crate. The result runs
  immediately under `ensemble run <name>.smoke` because
  `ensemble run` auto-discovers a `world.toml` in the cwd.
- Heavyweight (`--with-rust`): adds a Rust crate skeleton with a
  `WorldState` impl, a `Cargo.toml`, a `personas/` directory, and
  a typed scenarios entry. Reach for this when the world needs
  typed state with snapshot/restore semantics.
- Bound scenarios (`--world EXISTING_WORLD`): a scenarios package
  bound to an already-registered world. Skips the world boilerplate.

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
- `--with-rust`: scaffold the heavyweight shape with a Rust state
  crate. Mutually exclusive with `--world`.

Side effects: creates the directory tree and writes the files.
Prints the resulting root path plus the suggested next command.

Example:

```bash
ensemble init my_world
# scaffolds my_world.py, my_world/world.toml, my_world/scenarios/, README.md
# next: cd my_world && ensemble run my_world.smoke

ensemble init my_world --with-rust
# heavyweight scaffold with a Rust crate

ensemble init smoke_only --world plank
# scaffolds smoke_only/scenarios/ pointing at the already-installed plank
```

## ensemble run

```
ensemble run <scenario> [--world W] [--manifest M] [--package-dir D]
                        [--backend B] [--traces-dir TD] [--no-sync]
```

Runs a registered scenario and writes the trace into a per-run
directory at ``./traces/<run_id>/``. By default the CLI shells to
``uv run python -m ensemble.cli_run`` so the host project's
lockfile is honoured; ``--no-sync`` (or the ``ENSEMBLE_NO_SYNC``
env var) bypasses uv and uses the active python interpreter
directly. The bypass matters when the host's ``pyproject.toml``
has a yanked dependency or a stale lockfile that would crash uv
before the scenario even started.

World discovery happens in three stages: an explicit
``--package-dir`` always wins, otherwise the python entry point
looks for a ``world.toml`` in the cwd and auto-registers it (the
audit's two-step ceremony fix), and finally the worlds registry
at ``~/.ensemble/worlds.toml`` is consulted. The ``examples/plank``
directory is the final fallback for the README quickstart from
the repo root.

Arguments:

- ``<scenario>`` (positional, required): the scenario name as
  registered with ``@scenario`` or in a ``scenarios.toml``
  manifest.
- ``--world W``: the world the scenario constructs. Resolves
  through the worlds registry at ``~/.ensemble/worlds.toml``.
  Defaults to whatever the cwd's ``world.toml`` declares, then to
  the scenario's ``@scenario(..., world=...)`` declaration.
- ``--manifest M``: optional path to a ``scenarios.toml`` file the
  loader registers before lookup.
- ``--package-dir D``: directory holding the ``scenarios/`` python
  package to import. Defaults to the auto-discovered cwd or the
  registered world's directory. The loader tolerates a missing
  ``scenarios/__init__.py`` and walks the directory to import each
  ``*.py`` module by file path.
- ``--backend B``: ``mock`` | ``anthropic`` | ``openai`` | ``vllm``
  | ``auto``. Forwarded to the python entry point and printed as
  a system note before any LLM round trip. When the resolved
  backend is ``mock`` and the user did not explicitly request it,
  a loud bracketed banner names the consequence (canned
  deterministic stubs) and the fix.
- ``--traces-dir TD``: where to write the per-run dir. Defaults to
  ``./traces``.
- ``--no-sync``: skip ``uv run`` and use the active python (or the
  one in ``VIRTUAL_ENV/bin/python`` when set). Use when the host
  project's lockfile is broken or when you want to invoke the
  scenario from a venv that already has ``ensemble`` installed.

Side effects: creates ``<traces-dir>/<run_id>/`` containing
``trace.jsonl`` and ``meta.json``; appends a row to
``<traces-dir>/runs.jsonl``; creates a symlink at
``<traces-dir>/<safe_scenario_name>.jsonl`` pointing at the
latest run's trace. Prints a single JSON line on stdout
containing the scenario, the run id, the grader scores, and the
trace path. ``costs={...}`` is included when the run recorded
any cost.

Example:

```bash
ensemble run my_world.smoke
# {"scenario":"my_world.smoke","run_id":"20260520T143022_my_world_smoke_a1b2c3d4",
#  "scores":{"ok":1.0},"trace_path":"traces/20260520T143022_my_world_smoke_a1b2c3d4/trace.jsonl"}
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

## ensemble trace compare

```
ensemble trace compare <a> <b> [--port PORT] [--site SITE_DIR]
```

Serves a two-column browser view of two traces. Each column
renders one trace's message events (agent and user messages,
tool calls, tool results, system notes) as a chronological feed;
a sync-scroll toggle ties the columns by tick so equivalent
moments in both runs sit at the same vertical position. The
compare assets ship embedded, so the command works offline.

Pair with `ensemble runs compare` to first pick the two runs you
want to inspect, then open them visually.

Example:

```bash
ensemble trace compare \
  traces/20260520T1430_.../trace.jsonl \
  traces/20260520T1442_.../trace.jsonl
```

## ensemble models

Inspects the LLM backends ensemble knows about. Shells to
`uv run python -m ensemble.cli_models`.

### models list

```
ensemble models list
```

Prints a block per backend (`anthropic`, `openai`, `vllm`,
`mock`) with whether the relevant environment variable is set
and the model identifiers each backend accepts. The Anthropic
and OpenAI model lists come from the runtime crate's
`pricing.toml`, so the printed list is the authoritative set of
models the runtime can attribute USD cost to. vLLM model names
depend on what the endpoint serves; the section reports the
configured base URL.

Example:

```bash
ensemble models list
# [anthropic] ...   models: claude-sonnet-4-5, claude-opus-4-7, ...
# [openai]    ...   models: gpt-5, gpt-4o, ...
# [vllm]      ...   ENSEMBLE_VLLM_BASE_URL: not set
# [mock]      ...   no key required
```

## ensemble sweep

Runs the same scenario across a cartesian product of CLI flags
and environment variables. Shells to
`uv run python -m ensemble.cli_sweep`. See
[sweeps.md](sweeps.md) for the full TOML schema.

### sweep run

```
ensemble sweep run <config.toml> [--no-resume]
```

Loads the sweep config, expands the cartesian product, runs one
scenario invocation per cell (capped by `max_parallel`, default
1), and writes per-cell traces, per-cell `meta.json`, and a flat
`index.jsonl` to the sweep's traces directory.

- `<config.toml>` (positional, required): the sweep config.
- `--no-resume`: re-run cells whose `meta.json` already exists.
  Default behavior skips them so an interrupted sweep can be
  resumed cheaply.

Side effects: creates the sweep's traces directory and writes
one subdirectory per cell. Prints one line per cell to stderr
as cells complete (`[ok|fail|skipped] cell_id  scores: ...`)
and a JSON summary on stdout when finished. Exit code is 0 when
every cell succeeded, 1 otherwise.

## ensemble runs

Cross-run observability subcommands that read the per-run
`runs.jsonl` index. Shells to
`uv run python -m ensemble.cli_runs`. See
[observability.md](observability.md) for the on-disk layout and
the full subcommand semantics.

All four subcommands accept a global `--traces-dir D` to point
at a sweep's directory or any other index location (default:
`./traces`).

### runs list

```
ensemble runs list [--scenario NAME] [--limit N]
```

Prints recent runs as a table sorted oldest-first. `--scenario`
filters; `--limit` truncates to the last N rows.

### runs show

```
ensemble runs show <run_id_or_prefix>
```

Prints one run's meta as pretty-printed JSON. The argument can
be any unique prefix of the run id.

### runs compare

```
ensemble runs compare <a_id_or_prefix> <b_id_or_prefix>
```

Diffs two runs' scores side by side. Each metric shows the A
value, the B value, and the signed delta. Costs are dumped
unmodified at the bottom.

### runs export

```
ensemble runs export [--format json|csv]
```

Emits the full runs index. `json` is the default; `csv` flattens
scores into one column per metric so the file loads into pandas
without re-parsing JSON.

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
