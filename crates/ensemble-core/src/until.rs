use std::sync::Arc;

use crate::event::{EventLog, Tick};

/// An `Until` is a predicate evaluated each tick by the scheduler. When
/// it returns true the simulation halts. We accept a `Box<dyn Fn>` so
/// scenarios can compose closures freely.
pub struct Until {
    pub label: String,
    pub predicate: Arc<dyn Fn(&UntilCtx<'_>) -> bool + Send + Sync>,
}

impl Until {
    pub fn new<F>(label: impl Into<String>, f: F) -> Self
    where
        F: Fn(&UntilCtx<'_>) -> bool + Send + Sync + 'static,
    {
        Self {
            label: label.into(),
            predicate: Arc::new(f),
        }
    }

    pub fn check(&self, ctx: &UntilCtx<'_>) -> bool {
        (self.predicate)(ctx)
    }
}

pub struct UntilCtx<'a> {
    pub tick: Tick,
    pub log: &'a EventLog,
    pub events_seen: usize,
}

pub fn turn_count_exceeds(n: u64) -> Until {
    Until::new(format!("turn_count_exceeds({n})"), move |ctx| ctx.tick >= n)
}

pub fn any_of(parts: Vec<Until>) -> Until {
    let label = format!(
        "any_of({})",
        parts.iter().map(|u| u.label.as_str()).collect::<Vec<_>>().join(", ")
    );
    let arcs: Vec<Arc<dyn Fn(&UntilCtx<'_>) -> bool + Send + Sync>> =
        parts.into_iter().map(|u| u.predicate).collect();
    Until::new(label, move |ctx| arcs.iter().any(|p| p(ctx)))
}

pub fn all_of(parts: Vec<Until>) -> Until {
    let label = format!(
        "all_of({})",
        parts.iter().map(|u| u.label.as_str()).collect::<Vec<_>>().join(", ")
    );
    let arcs: Vec<Arc<dyn Fn(&UntilCtx<'_>) -> bool + Send + Sync>> =
        parts.into_iter().map(|u| u.predicate).collect();
    Until::new(label, move |ctx| arcs.iter().all(|p| p(ctx)))
}
