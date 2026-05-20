use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

/// Scaffold a new project. Two modes:
///
/// * `init <name>` creates a fresh world (rust crate skeleton + scenarios
///   dir + personas dir + world.toml manifest).
/// * `init --world <existing> <scenario_dir>` skips the world boilerplate
///   and just lays down a scenarios package that points at an
///   already-registered world.
pub fn init(name: &str, path: Option<&Path>, world: Option<&str>) -> Result<()> {
    let root: PathBuf = match path {
        Some(p) => p.to_path_buf(),
        None => PathBuf::from(name),
    };
    if root.exists() {
        anyhow::bail!("path already exists: {}", root.display());
    }

    if let Some(world_name) = world {
        scaffold_scenarios_dir(&root, name, world_name)?;
        println!(
            "scaffolded scenarios dir {name} at {} (bound to world {world_name:?})",
            root.display()
        );
        return Ok(());
    }

    scaffold_full_world(&root, name)?;
    println!("scaffolded {name} at {}", root.display());
    Ok(())
}

fn scaffold_scenarios_dir(root: &Path, name: &str, world: &str) -> Result<()> {
    fs::create_dir_all(root.join("scenarios"))?;
    fs::write(
        root.join("scenarios/__init__.py"),
        "from . import smoke  # noqa: F401\n",
    )?;
    fs::write(
        root.join("scenarios/smoke.py"),
        format!(
            "\"\"\"A smoke scenario for the {world} world.\"\"\"\n\nimport {world}  # noqa: F401  \
             registers the world with ensemble\nfrom ensemble import scenario\n\n\n\
             @scenario(\"{name}.smoke\", world=\"{world}\")\nasync def smoke(world):\n    \
             alice = world.spawn_user(id=\"alice\", model=\"user-model\")\n    rep = \
             world.spawn_agent(id=\"rep\", model=\"agent-model\")\n    alice.say(\"rep\", \
             \"hello\")\n    yield world.until(world.turn_count > 4)\n    yield {{\"ok\": 1.0}}\n"
        ),
    )?;
    fs::write(
        root.join("scenarios.toml"),
        format!(
            "[scenario.smoke]\nworld = \"{world}\"\nduration_turns = 4\n\n\
             [[scenario.smoke.agents]]\nid = \"rep\"\nmodel = \"agent-model\"\ntools = []\n\n\
             [scenario.smoke.graders]\nok = \"any_event\"\n"
        ),
    )?;
    Ok(())
}

fn scaffold_full_world(root: &Path, name: &str) -> Result<()> {
    fs::create_dir_all(root.join("world/src"))?;
    fs::create_dir_all(root.join(name))?;
    fs::create_dir_all(root.join("scenarios"))?;
    fs::create_dir_all(root.join("personas"))?;

    fs::write(
        root.join("world.toml"),
        format!(
            "[world]\nname = \"{name}\"\npython_package = \"{name}\"\nrust_crate = \"world\"\n\
             personas_dir = \"personas\"\n\n[[world.default_personas]]\nname = \"example\"\n"
        ),
    )?;
    fs::write(
        root.join("world/Cargo.toml"),
        format!(
            "[package]\nname = \"{name}\"\nversion = \"0.1.0\"\nedition = \"2021\"\n\n\
             [dependencies]\nensemble-core = {{ path = \"../crates/ensemble-core\" }}\n\
             ensemble-runtime = {{ path = \"../crates/ensemble-runtime\" }}\n\
             serde = {{ version = \"1\", features = [\"derive\"] }}\nserde_json = \"1\"\n"
        ),
    )?;
    fs::write(
        root.join("world/src/lib.rs"),
        format!(
            "//! {name}: a new world built on Ensemble.\n\nuse ensemble_core::prelude::*;\nuse \
             ensemble_core::error::{{RestoreError, ToolError}};\nuse serde::{{Deserialize, \
             Serialize}};\n\n#[derive(Default)]\npub struct State {{ /* TODO: your state */ }}\n\n\
             #[derive(Deserialize)]\npub enum Call {{ /* TODO: your tool calls */ }}\n\n\
             #[derive(Serialize, Clone)]\npub struct Effect;\n\n#[derive(Serialize)]\npub struct \
             Diff;\n\nimpl WorldState for State {{\n    type ToolCall = Call;\n    type \
             ToolEffect = Effect;\n    type Diff = Diff;\n\n    fn apply(&mut self, _call: \
             Call) -> Result<(Effect, Diff), ToolError> {{\n        \
             Err(ToolError::Execution(\"not implemented\".into()))\n    }}\n\n    fn \
             snapshot(&self) -> Vec<u8> {{ vec![] }}\n    fn restore(&mut self, _: &[u8]) -> \
             Result<(), RestoreError> {{ Ok(()) }}\n}}\n"
        ),
    )?;
    fs::write(
        root.join(format!("{name}/__init__.py")),
        format!(
            "\"\"\"The {name} world's python package.\n\nImporting this module is what registers \
             the world with ensemble. Scenarios that use {name} should `import {name}` near the \
             top of the file.\n\"\"\"\n\nfrom pathlib import Path\n\nfrom ensemble import \
             register_world\n\nPERSONAS_DIR = Path(__file__).resolve().parent.parent / \
             \"personas\"\n\n# TODO: register tools and predicates here.\n\
             register_world(\"{name}\", tools=[], predicates=[], \
             personas_dir=PERSONAS_DIR)\n"
        ),
    )?;
    fs::write(
        root.join("scenarios/__init__.py"),
        "from . import smoke  # noqa: F401\n",
    )?;
    fs::write(
        root.join("scenarios/smoke.py"),
        format!(
            "\"\"\"A smoke scenario for the {name} world.\"\"\"\n\nimport {name}  # noqa: F401  \
             registers the world\nfrom ensemble import scenario\n\n\n\
             @scenario(\"{name}.smoke\", world=\"{name}\")\nasync def smoke(world):\n    \
             alice = world.spawn_user(id=\"alice\")\n    rep = world.spawn_agent(id=\"rep\")\n    \
             alice.say(\"rep\", \"hello\")\n    yield world.until(world.turn_count > 4)\n    \
             yield {{\"ok\": 1.0}}\n"
        ),
    )?;
    fs::write(
        root.join("scenarios.toml"),
        format!(
            "[scenario.smoke]\nworld = \"{name}\"\nduration_turns = 4\n\n\
             [[scenario.smoke.agents]]\nid = \"rep\"\nmodel = \"agent-model\"\ntools = []\n\n\
             [scenario.smoke.graders]\nok = \"any_event\"\n"
        ),
    )?;
    fs::write(
        root.join("personas/example.toml"),
        "[persona]\nname = \"example\"\nmode = \"prompted\"\n\n[persona.system_prompt]\n\
         template = \"You are a placeholder persona. Replace this prompt.\"\n",
    )
    .context("write persona scaffold")?;
    fs::write(
        root.join("README.md"),
        format!(
            "# {name}\n\nA new world scaffolded by `ensemble init`. Implement `WorldState` in \
             `world/src/lib.rs`, register tools/predicates in `{name}/__init__.py`, then:\n\n```\n\
             ensemble worlds add {name} .\nensemble run {name}.smoke --world {name}\n```\n"
        ),
    )?;
    Ok(())
}
