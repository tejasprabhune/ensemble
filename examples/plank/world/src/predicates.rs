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
pub fn register_all(state: &Arc<Mutex<PlankState>>, preds: &PredicateRegistry) {
    let _ = state; // most predicates compute from the trace; some will use state later.

    // Plank inherits the core defaults (had_double_refund, any_event).
    let defaults = PredicateRegistry::with_defaults();
    for name in defaults.names() {
        // Copy each default into this registry. The closures are Arc'd
        // inside the source registry; we re-register a thin wrapper so
        // we don't have to expose `Predicate` directly.
        let src = defaults.clone();
        preds.register(name.clone(), move |ctx| {
            src.evaluate(&name, ctx).unwrap_or(false)
        });
    }

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

    // The per-user predicates take args = {"user_id": "<actor_id>"}.
    // They walk the trace to find the plank user_id ("u-...") that the
    // actor opened a ticket as, then check what happened to that user.

    preds.register("hidden_goal_resolved", |ctx| {
        let Some(actor_id) = ctx.args.get("user_id").and_then(|v| v.as_str()) else {
            return false;
        };
        let Some(plank_uid) = plank_user_id_for_actor(ctx.trace, actor_id) else {
            return false;
        };
        // Resolved if the agent issued a refund or escalated the ticket
        // the actor opened. Either action constitutes the agent acting
        // on the user's behalf.
        ctx.trace.iter().any(|e| match &e.payload {
            EventPayload::ToolResult {
                name,
                result,
                is_error,
                ..
            } if name == "issue_refund" && !is_error => result
                .get("data")
                .and_then(|d| d.get("user_id"))
                .and_then(|v| v.as_str())
                .map(|u| u == plank_uid)
                .unwrap_or(false),
            EventPayload::ToolResult {
                name,
                result,
                is_error,
                ..
            } if name == "escalate" && !is_error => {
                ticket_belongs_to(ctx.trace, result, &plank_uid)
            }
            _ => false,
        })
    });

    preds.register("was_redirected_to_upgrade", |ctx| {
        // Coarse: any agent message in the trace mentions upgrading.
        // The worked example pairs each user with a single agent so
        // this is accurate enough; richer routing would need recipient
        // metadata on bus events.
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

/// Find the plank `u-...` id the actor opened a ticket as. Returns the
/// `user_id` from the actor's first `open_ticket` ToolCall, or None.
fn plank_user_id_for_actor(trace: &[ensemble_core::event::Event], actor: &str) -> Option<String> {
    for e in trace {
        let Some(a) = e.actor.as_ref() else { continue };
        if a.as_str() != actor {
            continue;
        }
        if let EventPayload::ToolCall { name, args, .. } = &e.payload {
            if name == "open_ticket" {
                if let Some(u) = args.get("user_id").and_then(|v| v.as_str()) {
                    return Some(u.to_string());
                }
            }
        }
    }
    None
}

/// True if the ticket referenced by `escalate`'s result belongs to the
/// given plank user.
fn ticket_belongs_to(
    trace: &[ensemble_core::event::Event],
    escalate_result: &serde_json::Value,
    plank_uid: &str,
) -> bool {
    let Some(ticket_id) = escalate_result
        .get("data")
        .and_then(|d| d.get("ticket_id"))
        .and_then(|v| v.as_str())
    else {
        return false;
    };
    trace.iter().any(|e| {
        matches!(
            &e.payload,
            EventPayload::ToolCall { name, args, .. }
                if name == "open_ticket"
                    && args.get("ticket_id").and_then(|v| v.as_str()) == Some(ticket_id)
                    && args.get("user_id").and_then(|v| v.as_str()) == Some(plank_uid)
        )
    })
}
