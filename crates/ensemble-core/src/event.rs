use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::fs::{File, OpenOptions};
use tokio::io::AsyncWriteExt;
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

/// Optional sink that mirrors every appended event to a JSONL file.
///
/// The writer is owned by the EventLog and shared across clones, so a
/// `start_scheduler` clone that splits the log across actor tasks still
/// writes to the same file. Each append takes the writer lock, encodes
/// the event, writes one line, and flushes. Flushing per event makes
/// the file usable by a watcher while the run is still going.
#[derive(Clone)]
pub struct TraceFile {
    path: PathBuf,
    file: Arc<Mutex<File>>,
}

impl TraceFile {
    pub async fn create(path: impl AsRef<Path>) -> std::io::Result<Self> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                tokio::fs::create_dir_all(parent).await.ok();
            }
        }
        let file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(true)
            .open(&path)
            .await?;
        Ok(Self { path, file: Arc::new(Mutex::new(file)) })
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    async fn write_event(&self, event: &Event) -> std::io::Result<()> {
        let mut line = serde_json::to_string(event)
            .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
        line.push('\n');
        let mut file = self.file.lock().await;
        file.write_all(line.as_bytes()).await?;
        file.flush().await?;
        Ok(())
    }
}

#[derive(Clone, Default)]
pub struct EventLog {
    inner: Arc<Mutex<Vec<Event>>>,
    sink: Arc<Mutex<Option<TraceFile>>>,
}

impl EventLog {
    pub fn new() -> Self {
        Self::default()
    }

    /// Attach (or detach with `None`) a JSONL sink. Cheap; takes effect
    /// on the next append. Existing buffered events are not replayed
    /// to the file; the caller should attach the sink before the
    /// simulation starts to get a complete trace on disk.
    pub async fn set_sink(&self, sink: Option<TraceFile>) {
        *self.sink.lock().await = sink;
    }

    pub async fn sink_path(&self) -> Option<PathBuf> {
        self.sink.lock().await.as_ref().map(|s| s.path().to_path_buf())
    }

    pub async fn append(&self, event: Event) {
        self.inner.lock().await.push(event.clone());
        if let Some(sink) = self.sink.lock().await.clone() {
            if let Err(e) = sink.write_event(&event).await {
                eprintln!("ensemble: trace sink write failed: {e}");
            }
        }
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
