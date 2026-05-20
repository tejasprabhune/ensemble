//! Predicates Plank publishes for use in graders. They run against the
//! trace (and, when needed, against world state captured via the Arc
//! they close over).

use std::sync::Arc;

use parking_lot::Mutex;

use ensemble_core::event::EventPayload;
use ensemble_core::predicate::PredicateRegistry;

use crate::state::PlankState;

/// Register every predicate Plank exposes. Graders reference them by
/// name, e.g. `not had_double_refund` in a TOML manifest, or via
/// `world.evaluate_predicate("had_double_refund")` from Python.
pub fn register_all(state: &Arc<Mutex<PlankState>>, preds: &mut PredicateRegistry) {
    let _ = state; // most predicates compute from the trace; some will use state later.

    preds.register("had_double_refund", |ctx| {
        let mut seen = std::collections::HashSet::new();
        for e in ctx.trace {
            if let EventPayload::ToolCall { name, args, .. } = &e.payload {
                if name == "issue_refund" {
                    if let Some(uid) = args.get("user_id").and_then(|v| v.as_str()) {
                        if !seen.insert(uid.to_string()) {
                            return true;
                        }
                    }
                }
            }
        }
        false
    });

    preds.register("any_refund_issued", |ctx| {
        ctx.trace.iter().any(|e| {
            matches!(
                &e.payload,
                EventPayload::ToolResult { name, is_error, .. }
                    if name == "issue_refund" && !is_error
            )
        })
    });

    preds.register("any_escalation", |ctx| {
        ctx.trace.iter().any(|e| {
            matches!(
                &e.payload,
                EventPayload::ToolCall { name, .. } if name == "escalate"
            )
        })
    });

    preds.register("agent_recommended_upgrade", |ctx| {
        // Heuristic: agent suggested moving to a paid plan. Used by
        // graders that want to penalize unsolicited upsell.
        let needles = ["upgrade", "premium", "team plan", "enterprise plan"];
        ctx.trace.iter().any(|e| {
            if let EventPayload::AgentMessage { text } = &e.payload {
                let t = text.to_lowercase();
                needles.iter().any(|n| t.contains(n))
            } else {
                false
            }
        })
    });
}
