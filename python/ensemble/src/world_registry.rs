use once_cell::sync::Lazy;
use parking_lot::Mutex;
use std::collections::HashMap;

use ensemble_runtime::ToolRegistry;

/// What each registered world contributes when constructed: a fresh
/// per-instance tool registry. World state lives inside the closures
/// that the tools hold, so the registry plus its closures is enough
/// to keep state alive for the lifetime of the world instance.
pub struct WorldBundle {
    pub tools: ToolRegistry,
}

pub type WorldBuilder = fn() -> WorldBundle;

static REGISTRY: Lazy<Mutex<HashMap<String, WorldBuilder>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));

pub struct WorldRegistry;

impl WorldRegistry {
    pub fn register(name: &str, builder: WorldBuilder) {
        REGISTRY.lock().insert(name.into(), builder);
    }

    pub fn contains(name: &str) -> bool {
        REGISTRY.lock().contains_key(name)
    }

    pub fn build(name: &str) -> Option<WorldBundle> {
        let builder = REGISTRY.lock().get(name).copied()?;
        Some(builder())
    }
}
