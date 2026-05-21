/// Stage sink: buffers events and flushes them to the Stage HTTP API.
///
/// Enabled only when the `stage` cargo feature is active. When the feature
/// is off, all types and methods are zero-sized stubs so call sites compile
/// without cfg-gating every line.

#[cfg(feature = "stage")]
mod inner {
    use std::collections::VecDeque;
    use std::sync::{Arc, Mutex};
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    use anyhow::{anyhow, Result};
    use once_cell::sync::Lazy;
    use tokio::sync::Notify;

    use ensemble_core::event::{Event, EventSink};

    static CLIENT: Lazy<reqwest::Client> = Lazy::new(reqwest::Client::new);

    #[derive(Clone, Debug)]
    pub struct StageConfig {
        pub api_key: String,
        pub project: String,
        pub base_url: String,
    }

    impl StageConfig {
        pub fn org_slug(&self) -> &str {
            self.project.split('/').next().unwrap_or("")
        }

        pub fn project_slug(&self) -> &str {
            match self.project.find('/') {
                Some(i) => &self.project[i + 1..],
                None => &self.project,
            }
        }
    }

    struct Buffer {
        events: VecDeque<(u64, Event)>,
        next_seq: u64,
        shutdown: bool,
    }

    pub struct StageSink {
        config: StageConfig,
        run_id: String,
        buffer: Arc<Mutex<Buffer>>,
        notify: Arc<Notify>,
        created_at_ms: u64,
    }

    impl StageSink {
        pub async fn create(
            config: StageConfig,
            run_id: String,
            scenario: &str,
            world: &str,
            backend: &str,
            sweep_id: Option<String>,
        ) -> Result<(Arc<Self>, String)> {
            let org = config.org_slug().to_string();
            let proj = config.project_slug().to_string();
            let url = format!(
                "{}/v1/projects/{}/{}/runs",
                config.base_url.trim_end_matches('/'),
                org,
                proj
            );
            let mut body = serde_json::json!({
                "id": run_id,
                "scenario": scenario,
                "world": world,
                "backend": backend,
            });
            if let Some(ref sid) = sweep_id {
                body["sweep_id"] = serde_json::Value::String(sid.clone());
            }
            let api_key = config.api_key.clone();

            // Run the blocking HTTP call in a dedicated OS thread with its
            // own single-threaded runtime to avoid contention with the
            // global runtime's worker threads during block_on.
            let run_url = tokio::task::spawn_blocking(move || {
                blocking_post_json(&url, &api_key, &body)
            })
            .await
            .map_err(|e| anyhow!("stage: spawn_blocking join: {e}"))?
            .map_err(|e| anyhow!("stage: create run: {e}"))?;

            let buffer = Arc::new(Mutex::new(Buffer {
                events: VecDeque::new(),
                next_seq: 0,
                shutdown: false,
            }));
            let notify = Arc::new(Notify::new());
            let shutdown_notify = Arc::new(Notify::new());

            let sink = Arc::new(Self {
                config: config.clone(),
                run_id: run_id.clone(),
                buffer: buffer.clone(),
                notify: notify.clone(),
                created_at_ms: wall_time_ms(),
            });

            let flush_config = config.clone();
            let flush_run_id = run_id.clone();
            let flush_buffer = buffer;
            let flush_notify = notify;
            let flush_shutdown = shutdown_notify;

            tokio::spawn(async move {
                flush_task(
                    flush_config,
                    flush_run_id,
                    flush_buffer,
                    flush_notify,
                    flush_shutdown,
                )
                .await;
            });

            Ok((sink, run_url))
        }

        /// Async shutdown: signals the flush task, waits for the buffer
        /// to drain (up to ENSEMBLE_STAGE_FLUSH_TIMEOUT seconds), then
        /// POSTs a completed status. Must be called from within a tokio
        /// context (use `runtime.block_on(sink.shutdown_async(scores))`).
        pub async fn shutdown_async(&self, scores: Option<serde_json::Value>) -> u64 {
            {
                let mut buf = self.buffer.lock().expect("stage buffer lock");
                buf.shutdown = true;
            }
            self.notify.notify_one();

            let timeout_secs: u64 = std::env::var("ENSEMBLE_STAGE_FLUSH_TIMEOUT")
                .ok()
                .and_then(|v| v.parse().ok())
                .unwrap_or(5);

            let deadline = tokio::time::Instant::now()
                + Duration::from_secs(timeout_secs);
            loop {
                let empty = self.buffer.lock().expect("stage buffer lock").events.is_empty();
                if empty {
                    break;
                }
                if tokio::time::Instant::now() >= deadline {
                    break;
                }
                tokio::time::sleep(Duration::from_millis(50)).await;
            }

            let failed = self.buffer.lock().expect("stage buffer lock").events.len() as u64;
            let wall_ms = wall_time_ms().saturating_sub(self.created_at_ms) as i64;
            let _ = post_status(&self.config, &self.run_id, "completed", failed, scores, wall_ms).await;
            failed
        }
    }

    impl EventSink for StageSink {
        fn emit(&self, event: &Event) {
            let mut buf = self.buffer.lock().expect("stage buffer lock");
            let seq = buf.next_seq;
            buf.next_seq += 1;
            buf.events.push_back((seq, event.clone()));
            let should_wake = buf.events.len() >= 100;
            drop(buf);
            if should_wake {
                self.notify.notify_one();
            }
        }
    }

    async fn flush_task(
        config: StageConfig,
        run_id: String,
        buffer: Arc<Mutex<Buffer>>,
        notify: Arc<Notify>,
        _shutdown: Arc<Notify>,
    ) {
        let mut backoff_secs: u64 = 1;
        loop {
            tokio::select! {
                _ = notify.notified() => {},
                _ = tokio::time::sleep(Duration::from_secs(1)) => {},
            }

            let (batch, is_shutdown) = {
                let mut buf = buffer.lock().expect("stage buffer lock");
                let shutdown = buf.shutdown;
                let drain_count = buf.events.len().min(100);
                let batch: Vec<_> = buf.events.drain(..drain_count).collect();
                (batch, shutdown)
            };

            if batch.is_empty() {
                if is_shutdown {
                    break;
                }
                continue;
            }

            match post_events(&config, &run_id, &batch).await {
                Ok(_) => {
                    backoff_secs = 1;
                }
                Err(e) => {
                    eprintln!("stage: event flush failed: {e}");
                    {
                        let mut buf = buffer.lock().expect("stage buffer lock");
                        for item in batch.into_iter().rev() {
                            buf.events.push_front(item);
                        }
                    }
                    let sleep_secs = backoff_secs;
                    backoff_secs = (backoff_secs * 2).min(16);
                    tokio::time::sleep(Duration::from_secs(sleep_secs)).await;
                }
            }

            if is_shutdown {
                let remaining = buffer.lock().expect("stage buffer lock").events.len();
                if remaining == 0 {
                    break;
                }
            }
        }
    }

    fn log_traffic() -> bool {
        std::env::var("ENSEMBLE_STAGE_LOG_TRAFFIC").map(|v| v == "1").unwrap_or(false)
    }

    async fn post_events(
        config: &StageConfig,
        run_id: &str,
        batch: &[(u64, Event)],
    ) -> Result<()> {
        let client = client();
        let url = format!(
            "{}/v1/runs/{}/events",
            config.base_url.trim_end_matches('/'),
            run_id
        );
        let events_json: Vec<serde_json::Value> = batch
            .iter()
            .map(|(seq, ev)| {
                let wall_ms = wall_time_ms();
                let kind = event_kind(ev);
                let payload = serde_json::to_value(ev).unwrap_or(serde_json::Value::Null);
                serde_json::json!({
                    "sequence_number": seq,
                    "kind": kind,
                    "payload": payload,
                    "event_id": uuid::Uuid::new_v4().to_string(),
                    "wall_time_ms": wall_ms,
                })
            })
            .collect();
        let body = serde_json::json!({ "events": events_json });
        if log_traffic() {
            let body_str = serde_json::to_string(&body).unwrap_or_default();
            eprintln!("[stage-traffic] POST {url}");
            eprintln!("[stage-traffic] request body ({}B): {}", body_str.len(), &body_str[..body_str.len().min(2000)]);
        }
        let resp = client
            .post(&url)
            .bearer_auth(&config.api_key)
            .json(&body)
            .send()
            .await
            .map_err(|e| anyhow!("stage: post_events request: {e}"))?;
        let status = resp.status();
        let text = resp.text().await.unwrap_or_default();
        if log_traffic() {
            eprintln!("[stage-traffic] response {status}: {}", &text[..text.len().min(500)]);
        }
        if !status.is_success() {
            return Err(anyhow!("stage: post_events HTTP {status}: {text}"));
        }
        Ok(())
    }

    async fn post_status(
        config: &StageConfig,
        run_id: &str,
        status: &str,
        failed: u64,
        scores: Option<serde_json::Value>,
        wall_time_ms: i64,
    ) -> Result<()> {
        let client = client();
        let url = format!(
            "{}/v1/runs/{}/status",
            config.base_url.trim_end_matches('/'),
            run_id
        );
        let outcome = scores.unwrap_or_else(|| {
            if failed == 0 {
                serde_json::json!({})
            } else {
                serde_json::json!({ "flush_failed_count": failed })
            }
        });
        let body = serde_json::json!({
            "status": status,
            "outcome": outcome,
            "wall_time_ms": wall_time_ms,
        });
        if log_traffic() {
            let body_str = serde_json::to_string(&body).unwrap_or_default();
            eprintln!("[stage-traffic] POST {url}");
            eprintln!("[stage-traffic] request body: {body_str}");
        }
        let resp = client
            .post(&url)
            .bearer_auth(&config.api_key)
            .json(&body)
            .send()
            .await
            .map_err(|e| anyhow!("stage: post_status request: {e}"))?;
        let status_code = resp.status();
        let text = resp.text().await.unwrap_or_default();
        if log_traffic() {
            eprintln!("[stage-traffic] response {status_code}: {}", &text[..text.len().min(500)]);
        }
        if !status_code.is_success() {
            return Err(anyhow!("stage: post_status HTTP {status_code}: {text}"));
        }
        Ok(())
    }

    fn client() -> &'static reqwest::Client {
        &CLIENT
    }

    /// Synchronous HTTP POST using reqwest blocking. Called from spawn_blocking
    /// so it runs on a dedicated thread without nesting async runtimes.
    fn blocking_post_json(url: &str, api_key: &str, body: &serde_json::Value) -> Result<String> {
        static BLOCKING_CLIENT: Lazy<reqwest::blocking::Client> = Lazy::new(|| {
            reqwest::blocking::Client::builder()
                .timeout(Duration::from_secs(15))
                .build()
                .expect("stage blocking HTTP client")
        });

        let do_log = std::env::var("ENSEMBLE_STAGE_LOG_TRAFFIC").map(|v| v == "1").unwrap_or(false);
        if do_log {
            let body_str = serde_json::to_string(body).unwrap_or_default();
            eprintln!("[stage-traffic] POST {url}");
            eprintln!("[stage-traffic] request body: {body_str}");
        }

        let resp = BLOCKING_CLIENT
            .post(url)
            .header("Authorization", format!("Bearer {api_key}"))
            .header("Content-Type", "application/json")
            .json(body)
            .send()
            .map_err(|e| anyhow!("HTTP POST {url}: {e}"))?;

        let status = resp.status();
        let resp_body = resp.text().unwrap_or_default();

        if do_log {
            eprintln!("[stage-traffic] response {status}: {}", &resp_body[..resp_body.len().min(500)]);
        }

        if !status.is_success() {
            return Err(anyhow!("HTTP {}: {resp_body}", status.as_u16()));
        }

        let parsed: serde_json::Value = serde_json::from_str(&resp_body)
            .map_err(|e| anyhow!("parse response JSON: {e}"))?;
        let run_url = parsed
            .get("url")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string();
        Ok(run_url)
    }

    fn wall_time_ms() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64)
            .unwrap_or(0)
    }

    fn event_kind(ev: &Event) -> &'static str {
        match &ev.payload {
            ensemble_core::event::EventPayload::UserMessage { .. } => "user_message",
            ensemble_core::event::EventPayload::AgentMessage { .. } => "agent_message",
            ensemble_core::event::EventPayload::ToolCall { .. } => "tool_call",
            ensemble_core::event::EventPayload::ToolResult { .. } => "tool_result",
            ensemble_core::event::EventPayload::StateDiff { .. } => "state_diff",
            ensemble_core::event::EventPayload::Progress { .. } => "progress",
            ensemble_core::event::EventPayload::ToolTimeout { .. } => "tool_timeout",
            ensemble_core::event::EventPayload::Cost { .. } => "cost",
            ensemble_core::event::EventPayload::System { .. } => "system",
        }
    }
}

#[cfg(feature = "stage")]
pub use inner::{StageSink, StageConfig};

#[cfg(not(feature = "stage"))]
pub mod inner_stub {
    use ensemble_core::event::Event;

    #[derive(Clone, Debug, Default)]
    pub struct StageConfig {
        pub api_key: String,
        pub project: String,
        pub base_url: String,
    }

    pub struct StageSink;

    impl StageSink {
        pub fn emit(&self, _event: &Event) {}
        pub async fn shutdown_async(&self, _scores: Option<serde_json::Value>) -> u64 { 0 }
    }
}

#[cfg(not(feature = "stage"))]
pub use inner_stub::{StageSink, StageConfig};
