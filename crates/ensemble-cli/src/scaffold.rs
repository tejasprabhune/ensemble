use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

/// Scaffold a new project. Three shapes:
///
/// * `init <name>` (default): a pure-Python world. One module file,
///   a `world.toml`, a runnable smoke scenario, no Rust crate. The
///   common case for researchers writing a new world; the rust path
///   is the upgrade, not the starting point.
/// * `init <name> --with-rust`: the heavyweight shape with a Rust
///   crate, a `WorldState` skeleton, and the same Python plugin.
///   Reach for this when the world needs typed state with
///   snapshot/restore semantics, or when the tool dispatch must
///   live in compiled code.
/// * `init <scenarios_dir> --world <existing>`: a scenarios package
///   bound to an already-registered world. Skips the world
///   boilerplate.
pub fn init(
    name: &str,
    path: Option<&Path>,
    world: Option<&str>,
    with_rust: bool,
) -> Result<()> {
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

    if with_rust {
        scaffold_full_world_with_rust(&root, name)?;
        println!(
            "scaffolded {name} (with rust state) at {}\n\
             next: cd {} && ensemble run {name}.smoke",
            root.display(),
            root.display()
        );
    } else {
        scaffold_pure_python_world(&root, name)?;
        println!(
            "scaffolded {name} at {}\n\
             next: cd {} && ensemble run {name}.smoke",
            root.display(),
            root.display()
        );
    }
    Ok(())
}

fn scaffold_scenarios_dir(root: &Path, name: &str, world: &str) -> Result<()> {
    fs::create_dir_all(root.join("scenarios"))?;
    fs::write(
        root.join("scenarios/__init__.py"),
        "# Scenarios are auto-discovered: every *.py file in this dir is\n\
         # imported when `ensemble run` loads the world.\n",
    )?;
    fs::write(
        root.join("scenarios/smoke.py"),
        format!(
            "\"\"\"A smoke scenario for the {world} world.\"\"\"\n\n\
             import {world}  # noqa: F401  registers the world with ensemble\n\
             from ensemble import scenario\n\n\n\
             @scenario(\"{name}.smoke\", world=\"{world}\")\n\
             async def smoke(world):\n    \
             rep = world.spawn_agent(tools=[])\n    \
             world.opener(\"hello\", to=rep.id)\n    \
             yield world.until(world.turn_count > 4)\n    \
             yield {{\"ok\": 1.0}}\n"
        ),
    )?;
    Ok(())
}

/// Pure-Python world scaffold: one module file with the tool, one
/// scenarios file, a world.toml, no Rust crate. Runnable immediately
/// after `ensemble init <name>` because B2 lets `ensemble run`
/// discover `world.toml` in the cwd.
fn scaffold_pure_python_world(root: &Path, name: &str) -> Result<()> {
    fs::create_dir_all(root.join("scenarios"))?;

    fs::write(
        root.join("world.toml"),
        format!(
            "[world]\n\
             name = \"{name}\"\n\
             python_package = \"{name}\"\n\
             default_agent_model = \"claude-sonnet-4-5\"\n"
        ),
    )?;

    fs::write(
        root.join(format!("{name}.py")),
        format!(
            "\"\"\"The {name} world.\n\n\
             A single-file Python world. Importing this module registers it\n\
             with ensemble (the scenarios file does that for you). Add tools by\n\
             decorating plain functions with @tool: the name, description, and\n\
             JSON-Schema parameters come from the function itself.\n\
             \"\"\"\n\n\
             from ensemble import register_world, tool\n\n\n\
             @tool\n\
             def echo(text: str) -> str:\n    \
             \"\"\"Return the text back. Replace me with a real tool.\"\"\"\n    \
             return text\n\n\n\
             register_world(\n    \
             \"{name}\",\n    \
             tools=[echo],\n    \
             default_agent_model=\"claude-sonnet-4-5\",\n\
             )\n"
        ),
    )?;

    fs::write(
        root.join("scenarios/__init__.py"),
        "# Scenarios are auto-discovered: every *.py file in this dir is\n\
         # imported when `ensemble run` loads the world. Drop a new\n\
         # file here and `ensemble run <world>.<scenario_name>` picks\n\
         # it up without you editing this __init__.\n",
    )?;

    fs::write(
        root.join("scenarios/smoke.py"),
        format!(
            "\"\"\"A runnable smoke scenario for the {name} world.\n\n\
             Run with: ensemble run {name}.smoke\n\
             \"\"\"\n\n\
             import {name}  # noqa: F401  registers the world\n\
             from ensemble import scenario\n\n\n\
             @scenario(\"{name}.smoke\", world=\"{name}\")\n\
             async def smoke(world):\n    \
             rep = world.spawn_agent(tools=[\"echo\"])\n    \
             world.opener(\"say hi back\", to=rep.id)\n    \
             yield world.until(world.turn_count > 4)\n    \
             yield {{\"ok\": 1.0}}\n"
        ),
    )?;

    fs::write(
        root.join("README.md"),
        format!(
            "# {name}\n\n\
             A new ensemble world. The smoke scenario runs against the\n\
             deterministic mock backend with no setup:\n\n\
             ```\n\
             cd {name}\n\
             ensemble run {name}.smoke\n\
             ensemble trace view traces/{name}_smoke.jsonl\n\
             ```\n\n\
             ## Adding a real tool\n\n\
             Edit `{name}.py`. Tools are plain Python functions with type\n\
             hints; the decorator derives the JSON-Schema parameters,\n\
             description, and name from the function itself:\n\n\
             ```python\n\
             @tool\n\
             def lookup_user(user_id: str) -> dict:\n    \
             \"Return the user record by id.\"\n    \
             return db[user_id]\n\
             ```\n\n\
             Then add it to the `tools=[...]` list in `register_world(...)`\n\
             and reference it from a scenario's `spawn_agent(tools=[...])`.\n\n\
             ## Upgrading to a Rust state core\n\n\
             If your world needs typed state with snapshot/restore semantics,\n\
             regenerate the scaffold with `ensemble init <new-name> --with-rust`\n\
             and port over your Python tools.\n",
        ),
    )?;

    Ok(())
}

/// Heavyweight scaffold with a Rust crate. The old default, now
/// reached via --with-rust.
fn scaffold_full_world_with_rust(root: &Path, name: &str) -> Result<()> {
    fs::create_dir_all(root.join("world/src"))?;
    fs::create_dir_all(root.join(name))?;
    fs::create_dir_all(root.join("scenarios"))?;
    fs::create_dir_all(root.join("personas"))?;

    fs::write(
        root.join("world.toml"),
        format!(
            "[world]\nname = \"{name}\"\npython_package = \"{name}\"\nrust_crate = \"world\"\n\
             personas_dir = \"personas\"\ndefault_agent_model = \"claude-sonnet-4-5\"\n\n\
             [[world.default_personas]]\nname = \"example\"\n"
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
        "# Scenarios are auto-discovered: every *.py file in this dir is\n\
         # imported when `ensemble run` loads the world.\n",
    )?;
    fs::write(
        root.join("scenarios/smoke.py"),
        format!(
            "\"\"\"A smoke scenario for the {name} world.\"\"\"\n\n\
             import {name}  # noqa: F401  registers the world\n\
             from ensemble import scenario\n\n\n\
             @scenario(\"{name}.smoke\", world=\"{name}\")\n\
             async def smoke(world):\n    \
             rep = world.spawn_agent(tools=[])\n    \
             world.opener(\"hello\", to=rep.id)\n    \
             yield world.until(world.turn_count > 4)\n    \
             yield {{\"ok\": 1.0}}\n"
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
            "# {name}\n\nA new world scaffolded by `ensemble init --with-rust`. Implement `WorldState` in \
             `world/src/lib.rs`, register tools/predicates in `{name}/__init__.py`, then:\n\n```\n\
             ensemble run {name}.smoke\n```\n"
        ),
    )?;
    Ok(())
}
