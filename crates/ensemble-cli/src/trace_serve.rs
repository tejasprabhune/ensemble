use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

// The viewer ships as three files embedded directly in the binary.
// `ensemble trace view <path>` works without --site or any extra
// assets on disk; --site is still honoured for development when you
// want to edit the html or css and refresh.
const EMBEDDED_VIEWER_HTML: &str =
    include_str!("../../../site/viewer.html");
const EMBEDDED_VIEWER_JS: &str = include_str!("../../../site/viewer.js");
const EMBEDDED_VIEWER_CSS: &str = include_str!("../../../site/style.css");

pub fn serve(trace: &Path, port: u16, site: Option<&Path>) -> Result<()> {
    let trace_bytes = fs::read(trace)
        .with_context(|| format!("read trace from {}", trace.display()))?;

    // If --site was passed, mirror the old behaviour: write the trace
    // into <site>/trace.jsonl and serve files off disk so devs can
    // edit the html and reload. Otherwise serve everything from the
    // embedded copy and keep the trace in memory.
    let site_dir: Option<PathBuf> = match site {
        Some(s) => {
            if !s.exists() {
                anyhow::bail!("site directory not found: {}", s.display());
            }
            let baked = s.join("trace.jsonl");
            fs::write(&baked, &trace_bytes)
                .with_context(|| format!("bake trace into {}", baked.display()))?;
            Some(s.to_path_buf())
        }
        None => None,
    };

    let addr = format!("127.0.0.1:{port}");
    let listener =
        TcpListener::bind(&addr).with_context(|| format!("bind {addr}; is port in use?"))?;
    match &site_dir {
        Some(dir) => println!("serving {} on http://{addr}", dir.display()),
        None => println!("serving embedded viewer on http://{addr}"),
    }
    println!("press ctrl-c to stop");

    for stream in listener.incoming() {
        let mut stream = stream?;
        let mut buf = [0u8; 4096];
        let n = stream.read(&mut buf).unwrap_or(0);
        let req = String::from_utf8_lossy(&buf[..n]);
        let path = parse_request_path(&req).unwrap_or_else(|| "/".into());

        let (status, body, content_type) = match serve_one(&path, &site_dir, &trace_bytes) {
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
    trace_bytes: &[u8],
) -> Option<(Vec<u8>, &'static str)> {
    let mut p = req_path.trim_start_matches('/').to_string();
    if let Some(idx) = p.find('?') {
        p.truncate(idx);
    }
    if p.is_empty() {
        p = "viewer.html".into();
    }
    if p == "index.html" {
        p = "viewer.html".into();
    }

    if p == "trace.jsonl" {
        return Some((trace_bytes.to_vec(), "application/jsonl; charset=utf-8"));
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
