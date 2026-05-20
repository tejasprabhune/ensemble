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
    /// Serve a world's tools over MCP stdio.
    Serve {
        /// World to expose (must be registered via `ensemble worlds add`).
        #[arg(long)]
        world: String,
        /// Scenario to run while the server is up. Optional.
        #[arg(long)]
        scenario: Option<String>,
        /// Agent slot the connected client takes over. Optional.
        #[arg(long = "as-agent")]
        as_agent: Option<String>,
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
        Cmd::Init { name, path, world } => {
            scaffold::init(&name, path.as_deref(), world.as_deref())
        }
        Cmd::Run {
            scenario,
            world,
            manifest,
            package_dir,
        } => run_scenario(&scenario, world.as_deref(), manifest.as_deref(), package_dir.as_deref()),
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
    let mut cmd = Command::new("uv");
    cmd.args(["run", "python", "-m", "ensemble.cli_worlds"]);
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
        .context("invoking uv run python -m ensemble.cli_worlds; is uv on PATH?")?;
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
) -> Result<()> {
    // Default to the bundled plank scenarios when the caller did not
    // specify a package dir, so the README's quick-start works
    // out-of-the-box from a fresh clone.
    let pd: PathBuf = package_dir
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("examples/plank"));

    let mut cmd = Command::new("uv");
    cmd.args(["run", "python", "-m", "ensemble.cli_run"])
        .args(["--scenario", scenario])
        .args(["--world", world.unwrap_or("plank")])
        .args(["--package-dir"])
        .arg(&pd);
    if let Some(m) = manifest {
        cmd.args(["--manifest"]).arg(m);
    }

    let status = cmd
        .status()
        .context("invoking uv run python -m ensemble.cli_run; is uv on PATH?")?;
    if !status.success() {
        return Err(anyhow!("scenario run failed (exit {status})"));
    }
    Ok(())
}

fn mcp_subcommand(sub: McpCmd) -> Result<()> {
    let mut cmd = Command::new("uv");
    cmd.args(["run", "python", "-m", "ensemble.cli_mcp"]);
    match sub {
        McpCmd::Serve { world, scenario, as_agent } => {
            cmd.args(["serve", "--world", &world]);
            if let Some(s) = scenario {
                cmd.args(["--scenario", &s]);
            }
            if let Some(a) = as_agent {
                cmd.args(["--as-agent", &a]);
            }
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
