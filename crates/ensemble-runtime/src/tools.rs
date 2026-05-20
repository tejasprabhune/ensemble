use serde::Serialize;
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use ensemble_core::error::ToolError;

use crate::backend::ToolSchema;

/// One progress signal a tool emits during a long-running operation.
/// Buffered until the dispatch returns; the runtime flushes them to
/// the event log in order before the trailing ToolResult.
#[derive(Clone, Debug)]
pub struct ProgressEntry {
    pub fraction: f32,
    pub message: String,
}

/// Handed to a tool's run closure so the tool can report progress.
/// Cheap to clone; cloning shares the underlying buffer.
#[derive(Default, Clone)]
pub struct ProgressEmitter {
    entries: Arc<parking_lot::Mutex<Vec<ProgressEntry>>>,
}

impl ProgressEmitter {
    pub fn new() -> Self {
        Self::default()
    }

    /// Record a progress entry. Cheap and non-blocking. Safe to call
    /// from inside a `spawn_blocking` closure.
    pub fn emit(&self, fraction: f32, message: impl Into<String>) {
        self.entries.lock().push(ProgressEntry {
            fraction: fraction.clamp(0.0, 1.0),
            message: message.into(),
        });
    }

    /// Drain and return the buffered entries. Callers (i.e. the
    /// runtime's dispatch path) use this to flush to the bus.
    pub fn drain(&self) -> Vec<ProgressEntry> {
        std::mem::take(&mut *self.entries.lock())
    }
}

/// What a tool produced: a JSON `effect` always, plus an optional
/// `diff` that describes the world-state change. Tools that mutate
/// state should emit a diff so the trace viewer can render a row in
/// its state-change panel; pure lookups leave it `None`.
#[derive(Clone, Debug)]
pub struct ToolOutcome {
    pub effect: serde_json::Value,
    pub diff: Option<serde_json::Value>,
}

impl ToolOutcome {
    pub fn effect_only(effect: serde_json::Value) -> Self {
        Self { effect, diff: None }
    }

    pub fn with_diff(effect: serde_json::Value, diff: serde_json::Value) -> Self {
        Self { effect, diff: Some(diff) }
    }
}

/// A tool the runtime can dispatch on behalf of an agent. Tools are
/// world-agnostic: the closure receives parsed JSON args and a
/// `ProgressEmitter` and produces a `ToolOutcome`. World-specific code
/// lives in the closure.
///
/// `timeout` caps a single dispatch; if exceeded, the runtime cancels
/// the future, emits a `ToolTimeout` event, and the calling agent
/// sees a tool error. `resources` lists named locks the runtime must
/// acquire (via the world's [`ResourceManager`]) before invoking the
/// closure.
#[derive(Clone)]
pub struct Tool {
    pub schema: ToolSchema,
    pub timeout: Option<Duration>,
    pub resources: Vec<String>,
    pub run: Arc<
        dyn Fn(&serde_json::Value, &ProgressEmitter) -> Result<ToolOutcome, ToolError>
            + Send
            + Sync,
    >,
}

impl Tool {
    /// Lookup-style tool: returns only an effect, no state diff, no
    /// progress (the emitter argument is ignored).
    pub fn new<F, E>(
        name: impl Into<String>,
        description: impl Into<String>,
        parameters: serde_json::Value,
        run: F,
    ) -> Self
    where
        F: Fn(&serde_json::Value) -> Result<E, ToolError> + Send + Sync + 'static,
        E: Serialize,
    {
        Self::wrap(
            name,
            description,
            parameters,
            move |args, _emitter| {
                let effect = run(args)?;
                serde_json::to_value(effect)
                    .map(ToolOutcome::effect_only)
                    .map_err(|e| ToolError::Execution(format!("serialize: {e}")))
            },
        )
    }

    /// State-changing tool: returns both an effect and a diff. The diff
    /// is emitted as a StateDiff event by the runtime after the
    /// ToolResult. Use a diff shape the viewer's state panel expects
    /// (`{table, row_id, field, old, new}` or a list of those).
    pub fn new_with_diff<F, E, D>(
        name: impl Into<String>,
        description: impl Into<String>,
        parameters: serde_json::Value,
        run: F,
    ) -> Self
    where
        F: Fn(&serde_json::Value) -> Result<(E, D), ToolError> + Send + Sync + 'static,
        E: Serialize,
        D: Serialize,
    {
        Self::wrap(
            name,
            description,
            parameters,
            move |args, _emitter| {
                let (effect, diff) = run(args)?;
                let ev = serde_json::to_value(effect)
                    .map_err(|e| ToolError::Execution(format!("serialize effect: {e}")))?;
                let dv = serde_json::to_value(diff)
                    .map_err(|e| ToolError::Execution(format!("serialize diff: {e}")))?;
                Ok(ToolOutcome::with_diff(ev, dv))
            },
        )
    }

    /// Tool with progress reporting. The closure receives an emitter
    /// it calls during execution; the runtime flushes those entries
    /// to the trace once the closure returns. Use alongside
    /// `with_timeout` for long-running operations.
    pub fn new_with_progress<F, E>(
        name: impl Into<String>,
        description: impl Into<String>,
        parameters: serde_json::Value,
        run: F,
    ) -> Self
    where
        F: Fn(&serde_json::Value, &ProgressEmitter) -> Result<E, ToolError>
            + Send
            + Sync
            + 'static,
        E: Serialize,
    {
        Self::wrap(name, description, parameters, move |args, emitter| {
            let effect = run(args, emitter)?;
            serde_json::to_value(effect)
                .map(ToolOutcome::effect_only)
                .map_err(|e| ToolError::Execution(format!("serialize: {e}")))
        })
    }

    /// Internal constructor: wraps a closure that already speaks
    /// `(args, emitter) -> ToolOutcome`. The public factories build
    /// on top of this.
    fn wrap<F>(
        name: impl Into<String>,
        description: impl Into<String>,
        parameters: serde_json::Value,
        run: F,
    ) -> Self
    where
        F: Fn(&serde_json::Value, &ProgressEmitter) -> Result<ToolOutcome, ToolError>
            + Send
            + Sync
            + 'static,
    {
        Self {
            schema: ToolSchema {
                name: name.into(),
                description: description.into(),
                parameters,
            },
            timeout: None,
            resources: Vec::new(),
            run: Arc::new(run),
        }
    }

    /// Cap how long one dispatch may run. When exceeded the runtime
    /// emits a `ToolTimeout` event and the calling agent sees an
    /// error; the scenario continues.
    pub fn with_timeout(mut self, timeout: Duration) -> Self {
        self.timeout = Some(timeout);
        self
    }

    /// Declare the named resources this tool needs. The runtime
    /// acquires permits via the world's `ResourceManager` before
    /// invoking the closure, so concurrent dispatches that share a
    /// resource serialize.
    pub fn with_resources(mut self, resources: impl IntoIterator<Item = String>) -> Self {
        self.resources = resources.into_iter().collect();
        self
    }
}

/// A registry of tools agents can call. Cheap to clone (the underlying
/// map is shared behind an Arc) and uses interior mutability so plugins
/// can register tools after the registry has been handed to actors.
#[derive(Default, Clone)]
pub struct ToolRegistry {
    tools: Arc<parking_lot::RwLock<HashMap<String, Tool>>>,
}

impl ToolRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn register(&self, tool: Tool) {
        self.tools
            .write()
            .insert(tool.schema.name.clone(), tool);
    }

    pub fn get(&self, name: &str) -> Option<Tool> {
        self.tools.read().get(name).cloned()
    }

    pub fn schemas(&self) -> Vec<ToolSchema> {
        self.tools.read().values().map(|t| t.schema.clone()).collect()
    }

    pub fn names(&self) -> Vec<String> {
        let mut names: Vec<String> = self.tools.read().keys().cloned().collect();
        names.sort();
        names
    }

    /// Synchronous fast-path used by callers that already have a
    /// blocking thread (tests, the sync `User.act` path before phase
    /// 5 added timeouts). Skips the resource manager, runs the
    /// closure inline, and surfaces buffered progress on the returned
    /// outcome via the trailing flush path. Most callers should use
    /// [`dispatch_async`] instead.
    pub fn dispatch(
        &self,
        name: &str,
        args: &serde_json::Value,
    ) -> Result<ToolOutcome, ToolError> {
        let tool = self
            .tools
            .read()
            .get(name)
            .cloned()
            .ok_or_else(|| ToolError::UnknownTool(name.into()))?;
        let emitter = ProgressEmitter::new();
        (tool.run)(args, &emitter)
    }

    /// Async dispatch path. Acquires resource permits via the supplied
    /// `ResourceManager`, runs the (sync) closure inside
    /// `spawn_blocking`, and applies the tool's timeout. Returns the
    /// outcome plus the buffered progress entries so the caller can
    /// flush them to the event log alongside the trailing ToolResult.
    pub async fn dispatch_async(
        &self,
        name: &str,
        args: &serde_json::Value,
        resources: Option<&crate::resources::ResourceManager>,
    ) -> DispatchResult {
        let tool = match self.tools.read().get(name).cloned() {
            Some(t) => t,
            None => {
                return DispatchResult {
                    outcome: Err(ToolError::UnknownTool(name.into())),
                    progress: Vec::new(),
                    timed_out_after: None,
                };
            }
        };
        // Hold the permit guards for the duration of the dispatch.
        let _permits: Vec<_> = if let Some(rm) = resources {
            rm.acquire_all(&tool.resources).await
        } else {
            Vec::new()
        };
        let emitter = ProgressEmitter::new();
        let emitter_for_task = emitter.clone();
        let run = tool.run.clone();
        let args = args.clone();
        let join = tokio::task::spawn_blocking(move || {
            run(&args, &emitter_for_task)
        });
        let timeout = tool.timeout;
        let result = match timeout {
            Some(d) => tokio::time::timeout(d, join).await,
            None => Ok(join.await),
        };
        match result {
            Ok(Ok(outcome)) => DispatchResult {
                outcome,
                progress: emitter.drain(),
                timed_out_after: None,
            },
            Ok(Err(join_err)) => DispatchResult {
                outcome: Err(ToolError::Execution(format!(
                    "tool panicked: {join_err}"
                ))),
                progress: emitter.drain(),
                timed_out_after: None,
            },
            Err(_) => DispatchResult {
                outcome: Err(ToolError::Execution(format!(
                    "tool timed out after {:?}",
                    timeout.unwrap_or(Duration::ZERO)
                ))),
                progress: emitter.drain(),
                timed_out_after: timeout,
            },
        }
    }
}

/// What [`ToolRegistry::dispatch_async`] returns: the tool's outcome
/// (or error), any buffered progress entries, and whether the timeout
/// fired (with the configured cap).
pub struct DispatchResult {
    pub outcome: Result<ToolOutcome, ToolError>,
    pub progress: Vec<ProgressEntry>,
    pub timed_out_after: Option<Duration>,
}
