use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

pub fn init(name: &str, path: Option<&Path>) -> Result<()> {
    let root: PathBuf = match path {
        Some(p) => p.to_path_buf(),
        None => PathBuf::from(name),
    };
    if root.exists() {
        anyhow::bail!("path already exists: {}", root.display());
    }
    fs::create_dir_all(root.join("world/src"))?;
    fs::create_dir_all(root.join("scenarios"))?;
    fs::create_dir_all(root.join("personas"))?;

    fs::write(
        root.join("Cargo.toml"),
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
        root.join("scenarios/smoke.py"),
        format!(
            "\"\"\"A smoke scenario for the {name} world.\"\"\"\n\nfrom ensemble import \
             scenario\n\n\n@scenario(\"{name}.smoke\")\nasync def smoke(world):\n    alice = \
             world.spawn_user(id=\"alice\")\n    rep = world.spawn_agent(id=\"rep\")\n    \
             alice.say(\"rep\", \"hello\")\n    yield world.until(world.turn_count > 4)\n    \
             yield {{\"ok\": 1.0}}\n"
        ),
    )?;
    fs::write(
        root.join("scenarios.toml"),
        format!(
            "[scenario.smoke]\nworld = \"{name}\"\nduration_turns = 4\n\n\
             [[scenario.smoke.users]]\nid = \"alice\"\nmodel = \"user-model\"\n\n\
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
             `world/src/lib.rs`, then run `ensemble run {name}.smoke`.\n"
        ),
    )?;
    println!("scaffolded {name} at {}", root.display());
    Ok(())
}
