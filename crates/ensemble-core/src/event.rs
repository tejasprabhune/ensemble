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
    UserMessage { text: String },
    AgentMessage { text: String },
    ToolCall { name: String, args: serde_json::Value },
    ToolResult { name: String, result: serde_json::Value },
    StateDiff { diff: serde_json::Value },
    System { note: String },
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
