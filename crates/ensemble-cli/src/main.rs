use std::path::PathBuf;
use std::process::Command;

use anyhow::{anyhow, Context, Result};
use clap::{Parser, Subcommand};

mod scaffold;
mod trace_serve;

#[derive(Parser)]
#[command(
    name = "ensemble",
    version,
    about = "Ensemble CLI: scaffold worlds, run scenarios, view traces, kick off training."
)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Scaffold a new world project skeleton in the current directory.
    Init {
        /// Name of the new world or the scenario directory to create.
        name: String,
        /// Where to create the project (defaults to ./<name>).
        #[arg(long)]
        path: Option<PathBuf>,
        /// If set, scaffold a scenarios directory bound to an existing
        /// registered world (rather than creating a fresh world).
        #[arg(long)]
        world: Option<String>,
        /// Scaffold the heavyweight shape with a Rust state crate. Default
        /// is the pure-Python world: one module, one scenario, runnable
        /// in one command. Reach for --with-rust when the world needs
        /// typed state with snapshot/restore semantics.
        #[arg(long = "with-rust")]
        with_rust: bool,
    },
    /// Run a registered scenario and write the trace to ./traces/.
    Run {
        /// Scenario name as it appears in the @scenario registry or
        /// scenarios.toml manifest.
        scenario: String,
        /// World to construct. Resolves through the worlds registry
        /// (`ensemble worlds add <name> <path>`); pass the short name.
        #[arg(long)]
        world: Option<String>,
        /// Path to a scenarios.toml manifest to load (optional).
        #[arg(long)]
        manifest: Option<PathBuf>,
        /// Directory the python scenario package lives in. Defaults to
        /// the registered world's path.
        #[arg(long)]
        package_dir: Option<PathBuf>,
        /// LLM backend: mock | anthropic | openai | vllm | auto.
        /// Passed through to the python entry point.
        #[arg(long)]
        backend: Option<String>,
        /// Where to write the trace JSONL (default: ./traces).
        #[arg(long)]
        traces_dir: Option<PathBuf>,
        /// Skip `uv run` and invoke the current python interpreter
        /// directly. Useful when the host project's pyproject.toml
        /// has an unresolvable dependency or a stale lockfile: the
        /// scenario only needs `ensemble` itself on sys.path, which
        /// the active venv already has.
        #[arg(long)]
        no_sync: bool,
    },
    /// Trace-related subcommands.
    Trace {
        #[command(subcommand)]
        sub: TraceCmd,
    },
    /// Manage the registry of installed worlds at ~/.ensemble/worlds.toml.
    Worlds {
        #[command(subcommand)]
        sub: WorldsCmd,
    },
    /// Run an MCP server that exposes a world's tools to external agents.
    Mcp {
        #[command(subcommand)]
        sub: McpCmd,
    },
    /// Hand off persona training to the python pipeline.
    Train {
        /// Path to the persona TOML.
        persona: PathBuf,
        /// Compute backend.
        #[arg(long, value_parser = ["modal", "skypilot", "local"], default_value = "modal")]
        backend: String,
    },
}

#[derive(Subcommand)]
enum McpCmd {
    /// Serve a world's tools over MCP stdio. With --scenario and
    /// --as-agent, runs the scenario in the background and lets the
    /// connected MCP client drive the named agent slot.
    Serve {
        /// World to expose (must be registered via `ensemble worlds add`).
        #[arg(long)]
        world: String,
        /// Scenario to run while the server is up. Requires --as-agent.
        #[arg(long)]
        scenario: Option<String>,
        /// Agent slot the connected client takes over.
        #[arg(long = "as-agent")]
        as_agent: Option<String>,
        /// Directory holding the scenarios package to import (defaults
        /// to the world's directory).
        #[arg(long = "package-dir")]
        package_dir: Option<PathBuf>,
        /// LLM backend for the non-external actors in the scenario.
        #[arg(long, default_value = "mock")]
        backend: String,
    },
}

#[derive(Subcommand)]
enum WorldsCmd {
    /// List worlds in the registry.
    List,
    /// Register a world by local path. <name> must match the world's manifest.
    Add {
        name: String,
        path: PathBuf,
        /// Optional git URL (recorded but not used for cloning yet).
        #[arg(long)]
        git: Option<String>,
    },
    /// Unregister a world.
    Remove { name: String },
    /// Print a world's resolved manifest details.
    Show { name: String },
}

#[derive(Subcommand)]
enum TraceCmd {
    /// Serve the local trace viewer with the given trace baked in.
    View {
        /// Path to a JSONL trace file.
        trace: PathBuf,
        /// Port to bind the local viewer on.
        #[arg(long, default_value_t = 8765)]
        port: u16,
        /// Directory holding the static site to serve. Defaults to
        /// `./site` relative to the current working directory.
        #[arg(long)]
        site: Option<PathBuf>,
    },
}

fn main() -> Result<()> {
    tracing_subscriber::fmt::init();
    let cli = Cli::parse();
    match cli.cmd {
        Cmd::Init { name, path, world, with_rust } => {
            scaffold::init(&name, path.as_deref(), world.as_deref(), with_rust)
        }
        Cmd::Run {
            scenario,
            world,
            manifest,
            package_dir,
            backend,
            traces_dir,
            no_sync,
        } => run_scenario(
            &scenario,
            world.as_deref(),
            manifest.as_deref(),
            package_dir.as_deref(),
            backend.as_deref(),
            traces_dir.as_deref(),
            no_sync,
        ),
        Cmd::Trace { sub } => match sub {
            TraceCmd::View { trace, port, site } => {
                trace_serve::serve(&trace, port, site.as_deref())
            }
        },
        Cmd::Worlds { sub } => worlds_subcommand(sub),
        Cmd::Mcp { sub } => mcp_subcommand(sub),
        Cmd::Train { persona, backend } => train(&persona, &backend),
    }
}

fn worlds_subcommand(sub: WorldsCmd) -> Result<()> {
    let mut cmd = python_command(false);
    cmd.args(["-m", "ensemble.cli_worlds"]);
    match sub {
        WorldsCmd::List => {
            cmd.arg("list");
        }
        WorldsCmd::Add { name, path, git } => {
            cmd.arg("add").arg(&name).arg(&path);
            if let Some(g) = git {
                cmd.args(["--git", &g]);
            }
        }
        WorldsCmd::Remove { name } => {
            cmd.arg("remove").arg(&name);
        }
        WorldsCmd::Show { name } => {
            cmd.arg("show").arg(&name);
        }
    }
    let status = cmd
        .status()
        .context("invoking python -m ensemble.cli_worlds; is python on PATH?")?;
    if !status.success() {
        return Err(anyhow!("worlds subcommand failed (exit {status})"));
    }
    Ok(())
}

fn run_scenario(
    scenario: &str,
    world: Option<&str>,
    manifest: Option<&std::path::Path>,
    package_dir: Option<&std::path::Path>,
    backend: Option<&str>,
    traces_dir: Option<&std::path::Path>,
    no_sync: bool,
) -> Result<()> {
    // Default to the bundled plank scenarios when the caller did not
    // specify a package dir, so the README's quick-start works
    // out-of-the-box from a fresh clone.
    let pd: PathBuf = package_dir
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("examples/plank"));

    let mut cmd = python_command(no_sync);
    cmd.args(["-m", "ensemble.cli_run"])
        .args(["--scenario", scenario])
        .args(["--world", world.unwrap_or("plank")])
        .args(["--package-dir"])
        .arg(&pd);
    if let Some(m) = manifest {
        cmd.args(["--manifest"]).arg(m);
    }
    if let Some(b) = backend {
        cmd.args(["--backend", b]);
    }
    if let Some(td) = traces_dir {
        cmd.args(["--traces-dir"]).arg(td);
    }

    let status = cmd
        .status()
        .context("invoking python -m ensemble.cli_run; is python on PATH?")?;
    if !status.success() {
        return Err(anyhow!("scenario run failed (exit {status})"));
    }
    Ok(())
}

/// Build the leading command that ends up invoking python. The
/// default flow is `uv run python` so the host project's lockfile
/// is honoured; `--no-sync` (or `ENSEMBLE_NO_SYNC=1` in the env)
/// bypasses uv and uses the active interpreter directly. This
/// matters when the host's pyproject.toml has an unresolvable dep
/// or a yanked version that would crash uv before the scenario
/// even starts.
fn python_command(no_sync: bool) -> Command {
    let skip = no_sync || std::env::var("ENSEMBLE_NO_SYNC").is_ok();
    if skip {
        // Prefer the active virtualenv's python so the user's
        // dependencies and ensemble itself resolve from the same
        // place. Fall back to whatever `python` is on PATH so the
        // CLI still works outside a venv.
        if let Ok(venv) = std::env::var("VIRTUAL_ENV") {
            let candidate = PathBuf::from(venv).join("bin").join("python");
            if candidate.exists() {
                return Command::new(candidate);
            }
        }
        return Command::new("python");
    }
    let mut cmd = Command::new("uv");
    cmd.args(["run", "python"]);
    cmd
}

fn mcp_subcommand(sub: McpCmd) -> Result<()> {
    let mut cmd = Command::new("uv");
    cmd.args(["run", "python", "-m", "ensemble.cli_mcp"]);
    match sub {
        McpCmd::Serve {
            world,
            scenario,
            as_agent,
            package_dir,
            backend,
        } => {
            cmd.args(["serve", "--world", &world]);
            if let Some(s) = scenario {
                cmd.args(["--scenario", &s]);
            }
            if let Some(a) = as_agent {
                cmd.args(["--as-agent", &a]);
            }
            if let Some(p) = package_dir {
                cmd.arg("--package-dir").arg(p);
            }
            cmd.args(["--backend", &backend]);
        }
    }
    // MCP servers speak stdio with the connected client; let the
    // subprocess inherit our stdio so external MCP clients can drive
    // the server directly through `ensemble mcp serve`.
    let status = cmd
        .status()
        .context("invoking uv run python -m ensemble.cli_mcp; is uv on PATH?")?;
    if !status.success() {
        return Err(anyhow!("mcp subcommand failed (exit {status})"));
    }
    Ok(())
}

fn train(persona: &std::path::Path, backend: &str) -> Result<()> {
    let status = Command::new("uv")
        .args(["run", "ensemble-train", persona.to_str().unwrap_or(""), "--backend", backend])
        .status()
        .context("invoking uv run ensemble-train; is the train workspace synced?")?;
    if !status.success() {
        return Err(anyhow!("training failed (exit {status})"));
    }
    Ok(())
}
