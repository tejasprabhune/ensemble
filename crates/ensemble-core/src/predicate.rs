//! Named predicates a world can publish so scenarios and graders can
//! ask questions like "did this run leave a double refund" or "did
//! the agent recommend an upgrade". Worlds register predicates at
//! build time; scenarios evaluate them after the run completes.

use std::collections::HashMap;
use std::sync::Arc;

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
        Self { trace, args: serde_json::Value::Null }
    }

    pub fn with_args(trace: &'a [Event], args: serde_json::Value) -> Self {
        Self { trace, args }
    }
}

pub type Predicate = Arc<dyn Fn(&PredicateCtx<'_>) -> bool + Send + Sync>;

#[derive(Default, Clone)]
pub struct PredicateRegistry {
    preds: HashMap<String, Predicate>,
}

impl PredicateRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register<F>(&mut self, name: impl Into<String>, f: F)
    where
        F: Fn(&PredicateCtx<'_>) -> bool + Send + Sync + 'static,
    {
        self.preds.insert(name.into(), Arc::new(f));
    }

    pub fn names(&self) -> Vec<String> {
        let mut out: Vec<String> = self.preds.keys().cloned().collect();
        out.sort();
        out
    }

    pub fn evaluate(&self, name: &str, ctx: &PredicateCtx<'_>) -> Option<bool> {
        self.preds.get(name).map(|p| p(ctx))
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
        let mut reg = PredicateRegistry::new();
        reg.register("any_refund", |ctx| {
            ctx.trace.iter().any(|e| {
                matches!(&e.payload, EventPayload::ToolCall { name, .. } if name == "issue_refund")
            })
        });
        let trace = vec![evt(EventPayload::ToolCall {
            id: "x".into(),
            name: "issue_refund".into(),
            args: serde_json::json!({}),
        })];
        assert_eq!(reg.evaluate("any_refund", &PredicateCtx::new(&trace)), Some(true));
        assert_eq!(reg.evaluate("missing", &PredicateCtx::new(&trace)), None);
    }
}
