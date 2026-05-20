//! Named resource locks for tool dispatch.
//!
//! A world declares resources (e.g. `billing_db`, `gpu_0`) in
//! `world.toml`. Tools list the resources they need via
//! [`crate::Tool::with_resources`]; the runtime calls
//! [`ResourceManager::acquire_all`] before invoking the tool's closure.
//! Concurrent dispatches that share a resource serialize on the
//! underlying [`tokio::sync::Semaphore`].
//!
//! Process-wide [`shared`] storage lets two worlds with the same name
//! share semaphores, so two concurrent scenarios against the same
//! Plank world queue on `billing_db` together.

use std::collections::HashMap;
use std::sync::Arc;

use once_cell::sync::Lazy;
use parking_lot::Mutex;
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

/// What kind of access a named resource allows. The MVP supports
/// "exclusive" (a single permit, the default) and "shared with N
/// permits". A future revision can add reader/writer semantics.
#[derive(Clone, Copy, Debug)]
pub enum ResourceKind {
    Exclusive,
    Shared { permits: u32 },
}

impl ResourceKind {
    fn permits(self) -> u32 {
        match self {
            ResourceKind::Exclusive => 1,
            ResourceKind::Shared { permits } => permits.max(1),
        }
    }
}

#[derive(Default, Clone)]
pub struct ResourceManager {
    inner: Arc<Mutex<HashMap<String, Arc<Semaphore>>>>,
}

impl ResourceManager {
    pub fn new() -> Self {
        Self::default()
    }

    /// Declare a resource. If it already exists with a different
    /// permit count we leave the original in place (the first
    /// declaration wins) since changing semaphore capacity mid-run
    /// would break callers already holding permits.
    pub fn declare(&self, name: impl Into<String>, kind: ResourceKind) {
        let mut guard = self.inner.lock();
        guard
            .entry(name.into())
            .or_insert_with(|| Arc::new(Semaphore::new(kind.permits() as usize)));
    }

    /// Acquire one permit on each named resource. Returns the guards
    /// in declaration order. Resources are declared lazily as
    /// "exclusive" if a tool references an unknown name; this keeps
    /// the world.toml declaration optional for one-off use cases but
    /// reduces the risk of typos by recording the names that ever got
    /// used.
    pub async fn acquire_all(&self, names: &[String]) -> Vec<OwnedSemaphorePermit> {
        let mut sems: Vec<Arc<Semaphore>> = Vec::with_capacity(names.len());
        {
            let mut guard = self.inner.lock();
            for name in names {
                let sem = guard
                    .entry(name.clone())
                    .or_insert_with(|| Arc::new(Semaphore::new(1)))
                    .clone();
                sems.push(sem);
            }
        }
        // Acquire outside the mutex so we don't deadlock the registry
        // while waiting on a permit.
        let mut permits = Vec::with_capacity(sems.len());
        for sem in sems {
            let permit = sem.acquire_owned().await.expect("semaphore closed");
            permits.push(permit);
        }
        permits
    }

    /// Names of resources known to this manager.
    pub fn names(&self) -> Vec<String> {
        let guard = self.inner.lock();
        let mut out: Vec<String> = guard.keys().cloned().collect();
        out.sort();
        out
    }
}

static SHARED: Lazy<Mutex<HashMap<String, ResourceManager>>> =
    Lazy::new(|| Mutex::new(HashMap::new()));

/// Look up or create the process-global `ResourceManager` for the
/// world named `world_name`. Two `World(name)` instances share the
/// same manager so concurrent scenarios queue correctly on the same
/// `billing_db`.
pub fn shared(world_name: &str) -> ResourceManager {
    let mut guard = SHARED.lock();
    guard
        .entry(world_name.to_string())
        .or_insert_with(ResourceManager::new)
        .clone()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn shared_serializes_concurrent_acquires() {
        let rm = ResourceManager::new();
        rm.declare("db", ResourceKind::Exclusive);

        let order = Arc::new(Mutex::new(Vec::new()));

        let order1 = order.clone();
        let rm1 = rm.clone();
        let t1 = tokio::spawn(async move {
            let _p = rm1.acquire_all(&["db".to_string()]).await;
            order1.lock().push("t1-start");
            tokio::time::sleep(std::time::Duration::from_millis(40)).await;
            order1.lock().push("t1-end");
        });
        let order2 = order.clone();
        let rm2 = rm.clone();
        let t2 = tokio::spawn(async move {
            // Tiny delay to ensure t1 acquires first.
            tokio::time::sleep(std::time::Duration::from_millis(5)).await;
            let _p = rm2.acquire_all(&["db".to_string()]).await;
            order2.lock().push("t2-start");
            order2.lock().push("t2-end");
        });
        t1.await.unwrap();
        t2.await.unwrap();

        let final_order = order.lock().clone();
        let i1_end = final_order.iter().position(|s| *s == "t1-end").unwrap();
        let i2_start = final_order.iter().position(|s| *s == "t2-start").unwrap();
        assert!(i1_end < i2_start, "t2 should wait for t1: {final_order:?}");
    }
}
