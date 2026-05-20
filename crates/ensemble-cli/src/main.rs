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
        /// Name of the new world (snake_case).
        name: String,
        /// Where to create the project (defaults to ./<name>).
        #[arg(long)]
        path: Option<PathBuf>,
    },
    /// Run a registered scenario and write the trace to ./traces/.
    Run {
        /// Scenario name as it appears in the @scenario registry or
        /// scenarios.toml manifest.
        scenario: String,
        /// World to construct (defaults to whatever the scenario picks).
        #[arg(long)]
        world: Option<String>,
        /// Path to a scenarios.toml manifest to load (optional).
        #[arg(long)]
        manifest: Option<PathBuf>,
        /// Directory the python scenario package lives in. Defaults to
        /// `./examples/plank/scenarios` for the bundled demo.
        #[arg(long)]
        package_dir: Option<PathBuf>,
    },
    /// Trace-related subcommands.
    Trace {
        #[command(subcommand)]
        sub: TraceCmd,
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
        Cmd::Init { name, path } => scaffold::init(&name, path.as_deref()),
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
        Cmd::Train { persona, backend } => train(&persona, &backend),
    }
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
