use serde::{de::DeserializeOwned, Serialize};
use std::sync::Arc;
use tokio::sync::Mutex;

use crate::bus::Bus;
use crate::error::{RestoreError, ToolError};
use crate::event::EventPayload;
use crate::ids::ActorId;

pub trait WorldState: Send + Sync + 'static {
    type ToolCall: DeserializeOwned + Send;
    type ToolEffect: Serialize + Send;
    type Diff: Serialize + Send;

    fn apply(
        &mut self,
        call: Self::ToolCall,
    ) -> Result<(Self::ToolEffect, Self::Diff), ToolError>;

    fn snapshot(&self) -> Vec<u8>;
    fn restore(&mut self, snapshot: &[u8]) -> Result<(), RestoreError>;
}

/// A handle to the shared world state. Cloneable; all clones reference
/// the same underlying state behind a tokio Mutex.
pub struct WorldHandle<S: WorldState> {
    inner: Arc<Mutex<S>>,
}

impl<S: WorldState> Clone for WorldHandle<S> {
    fn clone(&self) -> Self {
        Self {
            inner: self.inner.clone(),
        }
    }
}

impl<S: WorldState> WorldHandle<S> {
    pub fn new(state: S) -> Self {
        Self {
            inner: Arc::new(Mutex::new(state)),
        }
    }

    pub async fn apply(
        &self,
        call: S::ToolCall,
    ) -> Result<(S::ToolEffect, S::Diff), ToolError> {
        let mut guard = self.inner.lock().await;
        guard.apply(call)
    }

    pub async fn snapshot(&self) -> Vec<u8> {
        self.inner.lock().await.snapshot()
    }

    pub async fn restore(&self, snapshot: &[u8]) -> Result<(), RestoreError> {
        self.inner.lock().await.restore(snapshot)
    }

    pub async fn with<R>(&self, f: impl FnOnce(&S) -> R) -> R {
        let guard = self.inner.lock().await;
        f(&*guard)
    }

    /// Apply a tool call and emit `ToolResult` + `StateDiff` events on
    /// the bus. The caller still owns the `ToolEffect` value (returned
    /// for further use); the diff is serialized into the log. `call_id`
    /// associates the result with the originating `ToolCall` event.
    pub async fn apply_and_log(
        &self,
        bus: &Bus,
        actor: ActorId,
        call_id: impl Into<String>,
        tool_name: &str,
        call: S::ToolCall,
    ) -> Result<S::ToolEffect, ToolError>
    where
        S::ToolEffect: Clone,
    {
        let (effect, diff) = self.apply(call).await?;
        let effect_json = serde_json::to_value(&effect).map_err(|e| {
            ToolError::Execution(format!("could not serialize tool effect: {e}"))
        })?;
        let diff_json = serde_json::to_value(&diff).map_err(|e| {
            ToolError::Execution(format!("could not serialize diff: {e}"))
        })?;
        let call_id = call_id.into();
        bus.append_event(
            Some(actor.clone()),
            EventPayload::ToolResult {
                id: call_id,
                name: tool_name.into(),
                result: effect_json,
                is_error: false,
                seed: false,
            },
        )
        .await;
        bus.append_event(
            Some(actor),
            EventPayload::StateDiff { diff: diff_json, seed: false },
        )
        .await;
        Ok(effect)
    }
}

/// `World` is the convenience alias users typically reach for. It owns
/// the shared mutable state and is the primary entry point for actors
/// to mutate the world via tool calls.
pub type World<S> = WorldHandle<S>;
