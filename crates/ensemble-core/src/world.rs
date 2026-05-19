use serde::{de::DeserializeOwned, Serialize};
use std::sync::Arc;
use tokio::sync::Mutex;

use crate::error::{RestoreError, ToolError};

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
}

/// `World` is the convenience alias users typically reach for. It owns
/// the shared mutable state and is the primary entry point for actors
/// to mutate the world via tool calls.
pub type World<S> = WorldHandle<S>;
