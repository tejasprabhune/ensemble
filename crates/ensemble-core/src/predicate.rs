//! Named predicates a world can publish so scenarios and graders can
//! ask questions like "did this run leave a double refund" or "did
//! the agent recommend an upgrade". Worlds register predicates at
//! build time; scenarios evaluate them after the run completes.

use std::collections::HashMap;
use std::sync::Arc;
use std::sync::RwLock;

use crate::event::Event;

/// Read-only view passed to a predicate. The trace is the full event
/// log at the moment of evaluation. `args` lets callers parameterise a
/// predicate (e.g. `{"user_id": "alice"}` for a per-user question);
/// most predicates ignore it. We pass a context struct (rather than a
/// bare slice) so future fields can be added without breaking
/// signatures.
pub struct PredicateCtx<'a> {
    pub trace: &'a [Event],
    pub args: serde_json::Value,
}

impl<'a> PredicateCtx<'a> {
    pub fn new(trace: &'a [Event]) -> Self {
        Self {
            trace,
            args: serde_json::Value::Null,
        }
    }

    pub fn with_args(trace: &'a [Event], args: serde_json::Value) -> Self {
        Self { trace, args }
    }
}

pub type Predicate = Arc<dyn Fn(&PredicateCtx<'_>) -> bool + Send + Sync>;

/// A registry of named predicates. Cheap to clone (shares state behind
/// an Arc) and uses interior mutability so worlds can register new
/// predicates after construction.
#[derive(Default, Clone)]
pub struct PredicateRegistry {
    preds: Arc<RwLock<HashMap<String, Predicate>>>,
}

impl PredicateRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    /// Build a registry pre-populated with a few generic predicates that
    /// every world inherits: `any_event` (true when the trace has at
    /// least one event), and `had_double_refund` (a generic
    /// `issue_refund` repetition check keyed by `args.user_id`).
    pub fn with_defaults() -> Self {
        let reg = Self::default();
        defaults::install(&reg);
        reg
    }

    pub fn register<F>(&self, name: impl Into<String>, f: F)
    where
        F: Fn(&PredicateCtx<'_>) -> bool + Send + Sync + 'static,
    {
        self.preds
            .write()
            .expect("predicate registry poisoned")
            .insert(name.into(), Arc::new(f));
    }

    pub fn names(&self) -> Vec<String> {
        let guard = self.preds.read().expect("predicate registry poisoned");
        let mut out: Vec<String> = guard.keys().cloned().collect();
        out.sort();
        out
    }

    pub fn evaluate(&self, name: &str, ctx: &PredicateCtx<'_>) -> Option<bool> {
        let guard = self.preds.read().expect("predicate registry poisoned");
        guard.get(name).map(|p| p(ctx))
    }
}

mod defaults {
    use super::PredicateRegistry;
    use crate::event::EventPayload;
    use std::collections::HashSet;

    pub fn install(reg: &PredicateRegistry) {
        reg.register("any_event", |ctx| !ctx.trace.is_empty());
        reg.register("had_double_refund", |ctx| {
            let mut seen = HashSet::new();
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
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::event::{Event, EventPayload};

    fn evt(payload: EventPayload) -> Event {
        Event {
            tick: 0,
            ts_ms: 0,
            actor: None,
            message_id: None,
            payload,
        }
    }

    #[test]
    fn counts_tool_calls() {
        let reg = PredicateRegistry::new();
        reg.register("any_refund", |ctx| {
            ctx.trace.iter().any(|e| {
                matches!(&e.payload, EventPayload::ToolCall { name, .. } if name == "issue_refund")
            })
        });
        let trace = vec![evt(EventPayload::ToolCall {
            id: "x".into(),
            name: "issue_refund".into(),
            args: serde_json::json!({}),
            seed: false,
        })];
        assert_eq!(
            reg.evaluate("any_refund", &PredicateCtx::new(&trace)),
            Some(true)
        );
        assert_eq!(reg.evaluate("missing", &PredicateCtx::new(&trace)), None);
    }
}
