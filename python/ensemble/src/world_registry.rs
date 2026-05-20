use once_cell::sync::Lazy;
use parking_lot::Mutex;
use std::collections::HashMap;

use ensemble_core::predicate::PredicateRegistry;
use ensemble_runtime::ToolRegistry;

/// What each registered world contributes when constructed: a fresh
/// per-instance tool registry and a per-instance predicate registry.
/// World state lives inside the closures that the tools and predicates
/// hold, so the bundle is enough to keep state alive for the lifetime
/// of the world instance.
pub struct WorldBundle {
    pub tools: ToolRegistry,
    pub predicates: PredicateRegistry,
}

pub type WorldBuilder = fn() -> WorldBundle;

static REGISTRY: Lazy<Mutex<HashMap<String, WorldBuilder>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));

pub struct WorldRegistry;

impl WorldRegistry {
    pub fn register(name: &str, builder: WorldBuilder) {
        REGISTRY.lock().insert(name.into(), builder);
    }

    pub fn build(name: &str) -> Option<WorldBundle> {
        let builder = REGISTRY.lock().get(name).copied()?;
        Some(builder())
    }
}
