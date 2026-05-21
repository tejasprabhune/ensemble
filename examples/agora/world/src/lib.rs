//! Agora: a small fake customer-service SaaS that doubles as the
//! worked example world for Ensemble. State is backed by SQLite (in
//! memory by default) and exposes six tools agents can call.

use std::sync::Arc;

use parking_lot::Mutex;

use ensemble_core::predicate::PredicateRegistry;
use ensemble_runtime::ToolRegistry;

pub mod predicates;
pub mod state;
pub mod tools;

pub use state::AgoraState;

/// Build a fresh state seeded with demo data, plus a tool registry and
/// a predicate registry whose closures hold an `Arc` to that state.
pub fn build() -> (Arc<Mutex<AgoraState>>, ToolRegistry, PredicateRegistry) {
    let state = Arc::new(Mutex::new(AgoraState::seed_default()));
    let tools = ToolRegistry::new();
    register_all(&state, &tools);
    let preds = PredicateRegistry::new();
    predicates::register_all(&state, &preds);
    (state, tools, preds)
}

/// Install Agora's tools onto an existing registry. Worlds may register
/// fewer or more; agents only see the schemas in their `tools=[...]`
/// list when they spawn.
pub fn register_all(state: &Arc<Mutex<AgoraState>>, tools: &ToolRegistry) {
    tools::open_ticket(state, tools);
    tools::lookup_user(state, tools);
    tools::lookup_ticket(state, tools);
    tools::issue_refund(state, tools);
    tools::escalate(state, tools);
    tools::search_kb(state, tools);
    tools::update_subscription(state, tools);
    tools::slow_billing_check(state, tools);
}
