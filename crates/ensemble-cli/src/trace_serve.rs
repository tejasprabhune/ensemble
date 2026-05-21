use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

// The viewer ships as embedded files baked into the binary.
// `ensemble trace view <path>` works without --site or any extra
// assets on disk; --site is still honoured for development.
const EMBEDDED_VIEWER_HTML: &str =
    include_str!("../../../site/viewer.html");
const EMBEDDED_VIEWER_JS: &str = include_str!("../../../site/viewer.js");
const EMBEDDED_VIEWER_CSS: &str = include_str!("../../../site/style.css");
// Shared viewer modules (DataSource abstraction + LocalJsonlSource).
const EMBEDDED_SHARED_VIEWER_JS: &str =
    include_str!("../../../shared/trace-viewer/viewer.js");
const EMBEDDED_SHARED_LOCAL_JS: &str =
    include_str!("../../../shared/trace-viewer/sources/local-jsonl.js");
const EMBEDDED_SHARED_STAGE_JS: &str =
    include_str!("../../../shared/trace-viewer/sources/stage-polling.js");
// Compare-mode viewer: two traces side by side, scroll-synced by tick.
const EMBEDDED_COMPARE_HTML: &str =
    include_str!("../../../site/compare.html");
const EMBEDDED_COMPARE_JS: &str =
    include_str!("../../../site/compare.js");

/// Bundle of trace bytes the server holds in memory. Single-trace
/// mode populates `primary` only; compare mode populates `primary`
/// (mirrored at /trace.jsonl and /trace_a.jsonl) plus `secondary`
/// at /trace_b.jsonl. Keeping both in one struct lets serve_one
/// switch on what the user asked for without a separate code path.
struct TraceBytes {
    primary: Vec<u8>,
    secondary: Option<Vec<u8>>,
}

pub fn serve(trace: &Path, port: u16, site: Option<&Path>) -> Result<()> {
    let trace_bytes = fs::read(trace)
        .with_context(|| format!("read trace from {}", trace.display()))?;
    let traces = TraceBytes { primary: trace_bytes, secondary: None };
    let site_dir = prepare_site_dir(site, &traces)?;
    serve_loop(port, site_dir, traces, /* compare = */ false)
}

pub fn serve_compare(a: &Path, b: &Path, port: u16, site: Option<&Path>) -> Result<()> {
    let a_bytes = fs::read(a)
        .with_context(|| format!("read trace from {}", a.display()))?;
    let b_bytes = fs::read(b)
        .with_context(|| format!("read trace from {}", b.display()))?;
    let traces = TraceBytes { primary: a_bytes, secondary: Some(b_bytes) };
    let site_dir = prepare_site_dir(site, &traces)?;
    serve_loop(port, site_dir, traces, /* compare = */ true)
}

fn prepare_site_dir(site: Option<&Path>, traces: &TraceBytes) -> Result<Option<PathBuf>> {
    let Some(s) = site else { return Ok(None); };
    if !s.exists() {
        anyhow::bail!("site directory not found: {}", s.display());
    }
    let primary = s.join("trace.jsonl");
    fs::write(&primary, &traces.primary)
        .with_context(|| format!("bake trace into {}", primary.display()))?;
    if let Some(b) = &traces.secondary {
        let a_path = s.join("trace_a.jsonl");
        let b_path = s.join("trace_b.jsonl");
        fs::write(&a_path, &traces.primary)
            .with_context(|| format!("bake trace into {}", a_path.display()))?;
        fs::write(&b_path, b)
            .with_context(|| format!("bake trace into {}", b_path.display()))?;
    }
    Ok(Some(s.to_path_buf()))
}

fn serve_loop(
    port: u16,
    site_dir: Option<PathBuf>,
    traces: TraceBytes,
    compare: bool,
) -> Result<()> {
    let addr = format!("127.0.0.1:{port}");
    let listener =
        TcpListener::bind(&addr).with_context(|| format!("bind {addr}; is port in use?"))?;
    match &site_dir {
        Some(dir) => println!("serving {} on http://{addr}", dir.display()),
        None if compare => println!("serving embedded compare viewer on http://{addr}"),
        None => println!("serving embedded viewer on http://{addr}"),
    }
    println!("press ctrl-c to stop");

    for stream in listener.incoming() {
        let mut stream = stream?;
        let mut buf = [0u8; 4096];
        let n = stream.read(&mut buf).unwrap_or(0);
        let req = String::from_utf8_lossy(&buf[..n]);
        let path = parse_request_path(&req).unwrap_or_else(|| "/".into());

        let (status, body, content_type) = match serve_one(&path, &site_dir, &traces, compare) {
            Some((body, ct)) => ("200 OK", body, ct),
            None => ("404 Not Found", b"not found".to_vec(), "text/plain"),
        };

        let header = format!(
            "HTTP/1.1 {}\r\nContent-Length: {}\r\nContent-Type: {}\r\nConnection: close\r\n\r\n",
            status,
            body.len(),
            content_type
        );
        let _ = stream.write_all(header.as_bytes());
        let _ = stream.write_all(&body);
    }
    Ok(())
}

fn serve_one(
    req_path: &str,
    site_dir: &Option<PathBuf>,
    traces: &TraceBytes,
    compare: bool,
) -> Option<(Vec<u8>, &'static str)> {
    let mut p = req_path.trim_start_matches('/').to_string();
    if let Some(idx) = p.find('?') {
        p.truncate(idx);
    }
    if p.is_empty() {
        p = if compare { "compare.html".into() } else { "viewer.html".into() };
    }
    if p == "index.html" {
        p = if compare { "compare.html".into() } else { "viewer.html".into() };
    }

    if p == "trace.jsonl" || p == "trace_a.jsonl" {
        return Some((traces.primary.clone(), "application/jsonl; charset=utf-8"));
    }
    if p == "trace_b.jsonl" {
        if let Some(b) = &traces.secondary {
            return Some((b.clone(), "application/jsonl; charset=utf-8"));
        }
        return None;
    }

    if let Some(dir) = site_dir {
        let resolved = sanitize_join(dir, &p);
        if let Ok(bytes) = fs::read(&resolved) {
            return Some((bytes, guess_content_type(&resolved)));
        }
        // Fall through to the embedded copy if the file is not on disk
        // (handy when --site points at a partial overlay).
    }

    match p.as_str() {
        "viewer.html" => Some((
            EMBEDDED_VIEWER_HTML.as_bytes().to_vec(),
            "text/html; charset=utf-8",
        )),
        "viewer.js" => Some((
            EMBEDDED_VIEWER_JS.as_bytes().to_vec(),
            "application/javascript; charset=utf-8",
        )),
        "style.css" => Some((
            EMBEDDED_VIEWER_CSS.as_bytes().to_vec(),
            "text/css; charset=utf-8",
        )),
        "compare.html" => Some((
            EMBEDDED_COMPARE_HTML.as_bytes().to_vec(),
            "text/html; charset=utf-8",
        )),
        "compare.js" => Some((
            EMBEDDED_COMPARE_JS.as_bytes().to_vec(),
            "application/javascript; charset=utf-8",
        )),
        "shared/trace-viewer/viewer.js" => Some((
            EMBEDDED_SHARED_VIEWER_JS.as_bytes().to_vec(),
            "application/javascript; charset=utf-8",
        )),
        "shared/trace-viewer/sources/local-jsonl.js" => Some((
            EMBEDDED_SHARED_LOCAL_JS.as_bytes().to_vec(),
            "application/javascript; charset=utf-8",
        )),
        "shared/trace-viewer/sources/stage-polling.js" => Some((
            EMBEDDED_SHARED_STAGE_JS.as_bytes().to_vec(),
            "application/javascript; charset=utf-8",
        )),
        _ => None,
    }
}

fn parse_request_path(req: &str) -> Option<String> {
    let line = req.lines().next()?;
    let mut parts = line.split_whitespace();
    parts.next()?;
    parts.next().map(|s| s.to_string())
}

fn sanitize_join(root: &Path, p: &str) -> PathBuf {
    let clean: PathBuf = Path::new(p)
        .components()
        .filter(|c| matches!(c, std::path::Component::Normal(_)))
        .collect();
    root.join(clean)
}

fn guess_content_type(path: &Path) -> &'static str {
    match path.extension().and_then(|e| e.to_str()) {
        Some("html") => "text/html; charset=utf-8",
        Some("css") => "text/css; charset=utf-8",
        Some("js") => "application/javascript; charset=utf-8",
        Some("json") => "application/json; charset=utf-8",
        Some("jsonl") => "application/jsonl; charset=utf-8",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        _ => "application/octet-stream",
    }
}
