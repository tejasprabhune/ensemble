use once_cell::sync::Lazy;
use parking_lot::Mutex;
use std::collections::HashSet;

/// Worlds register themselves by name during module init. The python
/// `World(name=...)` constructor consults this registry; passing an
/// unknown name raises `ValueError`.
static REGISTRY: Lazy<Mutex<HashSet<String>>> = Lazy::new(|| Mutex::new(HashSet::new()));

pub struct WorldRegistry;

impl WorldRegistry {
    pub fn register(name: &str) {
        REGISTRY.lock().insert(name.into());
    }

    pub fn contains(name: &str) -> bool {
        REGISTRY.lock().contains(name)
    }
}
