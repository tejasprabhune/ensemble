//! Plank: a small fake project-management SaaS that doubles as the
//! worked example world for Ensemble. State is backed by SQLite (in
//! memory by default) and exposes six tools agents can call.

use std::sync::Arc;

use parking_lot::Mutex;

use ensemble_runtime::ToolRegistry;

pub mod state;
pub mod tools;

pub use state::PlankState;

/// Build a fresh state seeded with demo data, plus a tool registry
/// whose closures hold an `Arc` to that state.
pub fn build() -> (Arc<Mutex<PlankState>>, ToolRegistry) {
    let state = Arc::new(Mutex::new(PlankState::seed_default()));
    let mut tools = ToolRegistry::new();
    register_all(&state, &mut tools);
    (state, tools)
}

/// Install Plank's six tools onto an existing registry.
pub fn register_all(state: &Arc<Mutex<PlankState>>, tools: &mut ToolRegistry) {
    tools::lookup_user(state, tools);
    tools::lookup_ticket(state, tools);
    tools::issue_refund(state, tools);
    tools::escalate(state, tools);
    tools::search_kb(state, tools);
    tools::update_subscription(state, tools);
}
