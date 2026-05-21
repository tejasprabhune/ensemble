use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::fs::{File, OpenOptions};
use tokio::io::AsyncWriteExt;
use tokio::sync::Mutex;

/// A synchronous sink that receives every event appended to an EventLog.
///
/// Implementations must be Send + Sync because EventLog is cloned across
/// async tasks. The emit call is synchronous and non-blocking; implementations
/// that do I/O should buffer internally and flush on a background task.
pub trait EventSink: Send + Sync + 'static {
    fn emit(&self, event: &Event);
}

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
    /// A tool invocation. `seed` is set when the call originates
    /// from scenario setup (`User.act` or `World.apply`) rather than
    /// from an actor's runtime turn, so trace consumers can tell
    /// seeded mutations apart from agent or user decisions made
    /// while the scheduler was running.
    ToolCall {
        id: String,
        name: String,
        args: serde_json::Value,
        #[serde(default)]
        seed: bool,
    },
    ToolResult {
        id: String,
        name: String,
        result: serde_json::Value,
        #[serde(default)]
        is_error: bool,
        /// Mirrors the `seed` flag of the originating ToolCall so a
        /// consumer that filters seeded events can drop the result
        /// without correlating on `id`.
        #[serde(default)]
        seed: bool,
    },
    StateDiff {
        diff: serde_json::Value,
        /// Same convention as ToolCall.seed: true when the diff was
        /// produced by a seeded tool dispatch.
        #[serde(default)]
        seed: bool,
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
    /// Open `path` as a trace sink, appending to it if it already
    /// exists. This is the default that `set_trace_path` from a
    /// python session expects, so reattaching to the same file does
    /// not silently discard the prior contents. Use
    /// [`Self::create_truncated`] for callers that want a fresh
    /// file (the CLI's `ensemble run` does this so each new run
    /// starts from an empty trace).
    pub async fn create(path: impl AsRef<Path>) -> std::io::Result<Self> {
        Self::open(path, false).await
    }

    /// Open `path` and truncate any existing contents. Use when the
    /// caller is starting a new run and intends to overwrite the
    /// previous trace at this path.
    pub async fn create_truncated(path: impl AsRef<Path>) -> std::io::Result<Self> {
        Self::open(path, true).await
    }

    async fn open(path: impl AsRef<Path>, truncate: bool) -> std::io::Result<Self> {
        let path = path.as_ref().to_path_buf();
        if let Some(parent) = path.parent() {
            if !parent.as_os_str().is_empty() {
                tokio::fs::create_dir_all(parent).await.ok();
            }
        }
        let file = OpenOptions::new()
            .create(true)
            .write(true)
            .truncate(truncate)
            .append(!truncate)
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

#[derive(Clone)]
pub struct EventLog {
    inner: Arc<Mutex<Vec<Event>>>,
    sink: Arc<Mutex<Option<TraceFile>>>,
    event_sink: Arc<std::sync::Mutex<Option<Arc<dyn EventSink>>>>,
}

impl Default for EventLog {
    fn default() -> Self {
        Self {
            inner: Arc::new(Mutex::new(Vec::new())),
            sink: Arc::new(Mutex::new(None)),
            event_sink: Arc::new(std::sync::Mutex::new(None)),
        }
    }
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

    /// Attach (or detach) a synchronous EventSink. Called from the python
    /// layer after StageSink::create succeeds. Uses std::sync::Mutex so
    /// it does not require async context.
    pub fn set_event_sink(&self, sink: Option<Arc<dyn EventSink>>) {
        *self.event_sink.lock().expect("event_sink lock") = sink;
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
        if let Ok(guard) = self.event_sink.lock() {
            if let Some(ref es) = *guard {
                es.emit(&event);
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
