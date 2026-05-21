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
    }

    impl StageSink {
        pub async fn create(
            config: StageConfig,
            run_id: String,
            scenario: &str,
            world: &str,
            backend: &str,
        ) -> Result<(Arc<Self>, String)> {
            let org = config.org_slug().to_string();
            let proj = config.project_slug().to_string();
            let url = format!(
                "{}/v1/projects/{}/{}/runs",
                config.base_url.trim_end_matches('/'),
                org,
                proj
            );
            let body = serde_json::json!({
                "id": run_id,
                "scenario": scenario,
                "world": world,
                "backend": backend,
            });
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
        /// context (use `runtime.block_on(sink.shutdown_async())`).
        pub async fn shutdown_async(&self) -> u64 {
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
            let _ = post_status(&self.config, &self.run_id, "completed", failed).await;
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
        let resp = client
            .post(&url)
            .bearer_auth(&config.api_key)
            .json(&body)
            .send()
            .await
            .map_err(|e| anyhow!("stage: post_events request: {e}"))?;
        if !resp.status().is_success() {
            let status = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(anyhow!("stage: post_events HTTP {status}: {text}"));
        }
        Ok(())
    }

    async fn post_status(config: &StageConfig, run_id: &str, status: &str, failed: u64) -> Result<()> {
        let client = client();
        let url = format!(
            "{}/v1/runs/{}/status",
            config.base_url.trim_end_matches('/'),
            run_id
        );
        let body = serde_json::json!({
            "status": status,
            "outcome": if failed == 0 { "success" } else { "partial" },
        });
        let resp = client
            .post(&url)
            .bearer_auth(&config.api_key)
            .json(&body)
            .send()
            .await
            .map_err(|e| anyhow!("stage: post_status request: {e}"))?;
        if !resp.status().is_success() {
            let status_code = resp.status();
            let text = resp.text().await.unwrap_or_default();
            return Err(anyhow!("stage: post_status HTTP {status_code}: {text}"));
        }
        Ok(())
    }

    fn client() -> &'static reqwest::Client {
        &CLIENT
    }

    /// Synchronous HTTP POST using raw TCP. Called from spawn_blocking so
    /// we avoid nesting a tokio runtime inside block_on.
    fn blocking_post_json(url: &str, api_key: &str, body: &serde_json::Value) -> Result<String> {
        use std::io::{BufRead, BufReader};
        use std::net::TcpStream;

        let (host, port, path) = parse_http_url(url)?;
        let body_str = body.to_string();
        let request = format!(
            "POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\nAuthorization: Bearer {api_key}\r\nContent-Type: application/json\r\nContent-Length: {len}\r\nConnection: close\r\n\r\n{body_str}",
            len = body_str.len(),
        );

        let stream = TcpStream::connect(format!("{host}:{port}"))
            .map_err(|e| anyhow!("TCP connect to {host}:{port}: {e}"))?;
        stream.set_read_timeout(Some(Duration::from_secs(15))).ok();
        {
            use std::io::Write as W;
            let mut w = &stream;
            w.write_all(request.as_bytes())
                .map_err(|e| anyhow!("TCP write: {e}"))?;
        }

        let mut reader = BufReader::new(&stream);

        let mut status_line = String::new();
        reader.read_line(&mut status_line)
            .map_err(|e| anyhow!("reading status line: {e}"))?;
        let status_code: u16 = status_line.split_whitespace().nth(1)
            .and_then(|s| s.parse().ok())
            .unwrap_or(0);

        let mut content_length: Option<usize> = None;
        loop {
            let mut line = String::new();
            reader.read_line(&mut line)
                .map_err(|e| anyhow!("reading header: {e}"))?;
            if line == "\r\n" || line.is_empty() {
                break;
            }
            let lower = line.to_ascii_lowercase();
            if lower.starts_with("content-length:") {
                content_length = lower["content-length:".len()..].trim().parse().ok();
            }
        }

        let resp_body = if let Some(len) = content_length {
            let mut buf = vec![0u8; len];
            use std::io::Read;
            reader.read_exact(&mut buf)
                .map_err(|e| anyhow!("reading body: {e}"))?;
            String::from_utf8_lossy(&buf).into_owned()
        } else {
            let mut buf = String::new();
            use std::io::Read;
            reader.read_to_string(&mut buf).ok();
            buf
        };

        if status_code < 200 || status_code >= 300 {
            return Err(anyhow!("HTTP {status_code}: {resp_body}"));
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

    fn parse_http_url(url: &str) -> Result<(String, u16, String)> {
        let without_scheme = url.strip_prefix("http://")
            .ok_or_else(|| anyhow!("only http:// URLs supported for Stage (got {url:?})"))?;
        let (authority, path) = without_scheme.split_once('/').map(|(a, p)| (a, format!("/{p}"))).unwrap_or((without_scheme, "/".to_string()));
        let (host, port) = if let Some((h, p)) = authority.split_once(':') {
            (h.to_string(), p.parse::<u16>().unwrap_or(80))
        } else {
            (authority.to_string(), 80u16)
        };
        Ok((host, port, path))
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
        pub async fn shutdown_async(&self) -> u64 { 0 }
    }
}

#[cfg(not(feature = "stage"))]
pub use inner_stub::{StageSink, StageConfig};
