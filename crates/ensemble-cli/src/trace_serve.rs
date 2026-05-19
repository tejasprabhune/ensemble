use std::fs;
use std::io::{Read, Write};
use std::net::TcpListener;
use std::path::{Path, PathBuf};

use anyhow::{Context, Result};

/// Serve the trace viewer with the given trace baked in. Writes the
/// trace JSONL into `<site>/trace.jsonl` (overwriting whatever was
/// there) so the viewer's static `fetch('trace.jsonl')` picks it up.
pub fn serve(trace: &Path, port: u16, site: Option<&Path>) -> Result<()> {
    let site_dir: PathBuf = site
        .map(|s| s.to_path_buf())
        .unwrap_or_else(|| PathBuf::from("site"));
    if !site_dir.exists() {
        anyhow::bail!("site directory not found: {}", site_dir.display());
    }
    let trace_bytes = fs::read(trace)
        .with_context(|| format!("read trace from {}", trace.display()))?;
    let baked = site_dir.join("trace.jsonl");
    fs::write(&baked, &trace_bytes)
        .with_context(|| format!("bake trace into {}", baked.display()))?;

    let addr = format!("127.0.0.1:{port}");
    let listener =
        TcpListener::bind(&addr).with_context(|| format!("bind {addr}; is port in use?"))?;
    println!("serving {} on http://{addr}", site_dir.display());
    println!("press ctrl-c to stop");
    for stream in listener.incoming() {
        let mut stream = stream?;
        let mut buf = [0u8; 4096];
        let n = stream.read(&mut buf).unwrap_or(0);
        let req = String::from_utf8_lossy(&buf[..n]);
        let path = parse_request_path(&req).unwrap_or_else(|| "/".into());
        let resolved = resolve_path(&site_dir, &path);
        match fs::read(&resolved) {
            Ok(body) => {
                let ct = guess_content_type(&resolved);
                let header = format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nContent-Type: {}\r\nConnection: close\r\n\r\n",
                    body.len(),
                    ct
                );
                let _ = stream.write_all(header.as_bytes());
                let _ = stream.write_all(&body);
            }
            Err(_) => {
                let body = b"not found";
                let header = format!(
                    "HTTP/1.1 404 Not Found\r\nContent-Length: {}\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                let _ = stream.write_all(header.as_bytes());
                let _ = stream.write_all(body);
            }
        }
    }
    Ok(())
}

fn parse_request_path(req: &str) -> Option<String> {
    let line = req.lines().next()?;
    let mut parts = line.split_whitespace();
    parts.next()?;
    parts.next().map(|s| s.to_string())
}

fn resolve_path(root: &Path, req_path: &str) -> PathBuf {
    let mut p = req_path.trim_start_matches('/').to_string();
    if p.is_empty() || p == "/" {
        p = "index.html".into();
    }
    // Strip query strings.
    if let Some(idx) = p.find('?') {
        p.truncate(idx);
    }
    // Defensive: drop any path-traversal segments. This is a local
    // dev server, but no reason to be sloppy.
    let clean: PathBuf = Path::new(&p)
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
