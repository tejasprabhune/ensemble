use serde::{Deserialize, Serialize};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::Mutex;

use crate::ids::{ActorId, MessageId};

pub type Tick = u64;

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Event {
    pub tick: Tick,
    pub ts_ms: u128,
    pub actor: Option<ActorId>,
    pub message_id: Option<MessageId>,
    pub payload: EventPayload,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum EventPayload {
    UserMessage {
        text: String,
    },
    AgentMessage {
        text: String,
    },
    ToolCall {
        id: String,
        name: String,
        args: serde_json::Value,
    },
    ToolResult {
        id: String,
        name: String,
        result: serde_json::Value,
        #[serde(default)]
        is_error: bool,
    },
    StateDiff {
        diff: serde_json::Value,
    },
    /// Progress signal emitted by a long-running tool. `fraction` is a
    /// 0.0..=1.0 estimate of completion; `message` is short
    /// human-readable text. Flushed to the log after the tool dispatch
    /// returns (or after a timeout fires) in the MVP; a later revision
    /// can stream them live.
    Progress {
        id: String,
        tool: String,
        fraction: f32,
        message: String,
    },
    /// Emitted when a tool's run exceeded its declared timeout. The
    /// scenario continues; the calling agent sees a tool error.
    ToolTimeout {
        id: String,
        name: String,
        after_ms: u64,
    },
    /// A cost annotation flushed to the trace after the tool that
    /// produced it (or by an LLM backend wrapping a completion call).
    /// `running_total` is the world's running total for this unit
    /// after this annotation was applied.
    Cost {
        unit: String,
        amount: f64,
        running_total: f64,
    },
    System {
        note: String,
    },
}

#[derive(Clone, Default)]
pub struct EventLog {
    inner: Arc<Mutex<Vec<Event>>>,
}

impl EventLog {
    pub fn new() -> Self {
        Self::default()
    }

    pub async fn append(&self, event: Event) {
        self.inner.lock().await.push(event);
    }

    pub async fn snapshot(&self) -> Vec<Event> {
        self.inner.lock().await.clone()
    }

    pub async fn len(&self) -> usize {
        self.inner.lock().await.len()
    }

    pub async fn is_empty(&self) -> bool {
        self.inner.lock().await.is_empty()
    }

    pub async fn to_jsonl(&self) -> Result<String, serde_json::Error> {
        let events = self.snapshot().await;
        let mut out = String::new();
        for e in events {
            out.push_str(&serde_json::to_string(&e)?);
            out.push('\n');
        }
        Ok(out)
    }
}

pub fn now_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0)
}
