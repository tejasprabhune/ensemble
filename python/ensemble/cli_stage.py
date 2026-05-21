"""Stage CLI subcommands: login, logout, whoami, projects."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from typing import List, Optional

from .stage import (
    PROD_BASE_URL,
    _CREDS_PATH,
    _load_toml_file,
    write_credentials,
    write_project_toml,
)


def _is_uuid(s: str) -> bool:
    import uuid as _uuid_mod
    try:
        _uuid_mod.UUID(s)
        return True
    except (ValueError, AttributeError):
        return False


def _use_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


class _C:
    """ANSI color codes approximating the Stage website palette."""

    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    ACCENT = "\033[38;5;179m"   # warm amber  (#B8956A)
    MUTED  = "\033[38;5;245m"   # gray        (#8A827A)
    OK     = "\033[38;5;71m"    # green       (#5A9467)
    ERR    = "\033[38;5;167m"   # red         (#C45A5A)
    WARN   = "\033[38;5;178m"   # orange      (#C49A3A)

    @classmethod
    def disable(cls) -> None:
        for attr in ("RESET", "BOLD", "DIM", "ACCENT", "MUTED", "OK", "ERR", "WARN"):
            setattr(cls, attr, "")


if not _use_color():
    _C.disable()


def _kv(key: str, value: str, width: int = 14) -> str:
    return f"  {_C.MUTED}{key:<{width}}{_C.RESET}  {value}"


def _status_badge(status: str) -> str:
    s = status.lower()
    if s == "completed":
        return f"{_C.OK}completed{_C.RESET}"
    if s == "running":
        return f"{_C.ACCENT}running{_C.RESET}"
    if s in ("failed", "cancelled"):
        return f"{_C.ERR}{s}{_C.RESET}"
    return f"{_C.MUTED}{s}{_C.RESET}"


def _bearer_request(url: str, api_key: str, method: str = "GET", body: Optional[bytes] = None):
    """Make an authenticated HTTP request, return parsed JSON or raise."""
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode()
        _print_http_error(e.code, body_text, url)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"{_C.ERR}stage: cannot reach server{_C.RESET} {_C.MUTED}({e.reason}){_C.RESET}", file=sys.stderr)
        print(f"  Check that ENSEMBLE_STAGE_BASE_URL is correct and the server is reachable.", file=sys.stderr)
        sys.exit(1)


def _print_http_error(code: int, body_text: str, url: str = "") -> None:
    try:
        detail = json.loads(body_text).get("error", {}).get("message", body_text)
    except (json.JSONDecodeError, AttributeError):
        detail = body_text.strip()

    hints = {
        401: "Your API key is missing or has been revoked. Run `ensemble stage login` to authenticate.",
        403: (
            "Your API key does not have permission for this operation. "
            "Push-scoped keys can push runs, sweeps, and training metrics. "
            "Run `ensemble stage login` to get a fresh key, or create an admin-scoped key at /me."
        ),
        404: "The resource was not found. Check that the org and project slugs are correct.",
        409: "A resource with this identifier already exists.",
    }
    hint = hints.get(code, "")
    print(f"{_C.ERR}stage: {code} {_http_status_name(code)}{_C.RESET}  {_C.MUTED}{detail}{_C.RESET}", file=sys.stderr)
    if hint:
        print(f"  {hint}", file=sys.stderr)


def _http_status_name(code: int) -> str:
    return {401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
            409: "Conflict", 422: "Unprocessable", 500: "Internal Server Error"}.get(code, "")


def _load_credentials() -> tuple[str, str]:
    """Return (api_key, base_url) from credentials file or env, exit on missing."""
    import os
    api_key = os.environ.get("ENSEMBLE_STAGE_API_KEY", "").strip()
    base_url = os.environ.get("ENSEMBLE_STAGE_BASE_URL", "").strip()
    if not api_key:
        creds = _load_toml_file(_CREDS_PATH)
        api_key = creds.get("credentials", {}).get("api_key", "").strip()
        base_url = base_url or creds.get("credentials", {}).get("base_url", "").strip()
    if not api_key:
        print(
            "stage: not logged in. Run 'ensemble stage login' first, "
            "or set ENSEMBLE_STAGE_API_KEY.",
            file=sys.stderr,
        )
        sys.exit(1)
    return api_key, (base_url or PROD_BASE_URL)


def cmd_login(args) -> int:
    """Open OAuth browser flow and write credentials to ~/.ensemble/stage.toml."""
    import http.server
    import threading
    import webbrowser

    base_url = getattr(args, "base_url", None) or PROD_BASE_URL
    result: dict = {}
    ready = threading.Event()
    done = threading.Event()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            api_keys = params.get("api_key", [])
            if api_keys:
                result["api_key"] = api_keys[0]
            redirect_target = f"{base_url}/auth/cli/done"
            body = (
                f'<html><head><meta http-equiv="refresh" content="0;url={redirect_target}"></head>'
                f'<body>Redirecting...</body></html>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            done.set()

        def log_message(self, fmt, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = server.server_address[1]

    def serve():
        ready.set()
        server.handle_request()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ready.wait()

    oauth_url = f"{base_url}/auth/cli?callback=http://127.0.0.1:{port}/callback"
    print(f"Opening browser to: {oauth_url}", file=sys.stderr)
    try:
        webbrowser.open(oauth_url)
    except Exception:
        print(f"Could not open browser. Visit: {oauth_url}", file=sys.stderr)

    print("Waiting for authentication...", file=sys.stderr)
    done.wait(timeout=120)
    server.server_close()

    api_key = result.get("api_key", "")
    if not api_key:
        print(f"{_C.ERR}stage: login timed out or was cancelled.{_C.RESET}", file=sys.stderr)
        return 1

    user_login = ""
    org_slug = ""
    existing_projects: list = []
    try:
        me = _bearer_request(f"{base_url}/v1/me", api_key)
        user_login = me.get("github_login", "")
        org_slug = me.get("default_org_slug", "")
    except SystemExit:
        pass

    if org_slug:
        try:
            org_data = _bearer_request(f"{base_url}/v1/orgs/{org_slug}", api_key)
            existing_projects = org_data.get("projects", [])
        except SystemExit:
            pass

    write_credentials(api_key, base_url=base_url, user_login=user_login)

    name = f"{_C.BOLD}{_C.ACCENT}{user_login}{_C.RESET}" if user_login else f"{_C.MUTED}(unknown){_C.RESET}"
    print(f"\n{_C.OK}Logged in{_C.RESET} as {name}")
    print(_kv("org", org_slug or "(unknown)"))
    print(_kv("credentials", str(_CREDS_PATH)))
    if base_url and base_url != PROD_BASE_URL:
        print(_kv("server", base_url))

    print()
    if existing_projects:
        print(f"  {_C.MUTED}Your projects:{_C.RESET}")
        for p in existing_projects[:5]:
            slug = p.get("slug", "?")
            print(f"    {_C.ACCENT}{org_slug}/{slug}{_C.RESET}")
        print()
        print(f"  To push runs to a project, set:")
        print(f"    {_C.BOLD}export ENSEMBLE_STAGE_API_KEY={api_key}{_C.RESET}")
        if existing_projects:
            first = existing_projects[0].get("slug", "")
            print(f"    {_C.BOLD}export ENSEMBLE_STAGE_PROJECT={org_slug}/{first}{_C.RESET}")
    else:
        print(f"  {_C.MUTED}No projects yet.{_C.RESET} Create one:")
        print(f"    ensemble stage projects create {org_slug or 'your-org'}/my-project")
        print()
        print(f"  Then set:")
        print(f"    {_C.BOLD}export ENSEMBLE_STAGE_API_KEY={api_key}{_C.RESET}")
        print(f"    {_C.BOLD}export ENSEMBLE_STAGE_PROJECT={org_slug or 'your-org'}/my-project{_C.RESET}")

    return 0


def cmd_logout(_args) -> int:
    """Remove credentials from ~/.ensemble/stage.toml."""
    if _CREDS_PATH.exists():
        _CREDS_PATH.unlink()
        print(f"{_C.OK}Logged out.{_C.RESET} Removed {_C.MUTED}{_CREDS_PATH}{_C.RESET}")
    else:
        print(f"{_C.MUTED}Not logged in.{_C.RESET}")
    return 0


def cmd_whoami(_args) -> int:
    """Call GET /v1/me and print user info."""
    api_key, base_url = _load_credentials()
    data = _bearer_request(f"{base_url}/v1/me", api_key)
    login = data.get("github_login", "")
    email = data.get("email", "")
    org = data.get("default_org_slug", "")
    print(_kv("user", f"{_C.BOLD}{_C.ACCENT}{login}{_C.RESET}"))
    if email:
        print(_kv("email", email))
    if org:
        print(_kv("org", org))
    if base_url != PROD_BASE_URL:
        print(_kv("server", base_url))
    return 0


def cmd_projects_list(_args) -> int:
    """List accessible projects."""
    api_key, base_url = _load_credentials()
    me = _bearer_request(f"{base_url}/v1/me", api_key)
    org = me.get("default_org_slug", "")
    if not org:
        print("stage: no default org found. Use 'ensemble stage whoami' to debug.", file=sys.stderr)
        return 1
    data = _bearer_request(f"{base_url}/v1/orgs/{org}", api_key)
    projects = data.get("projects", [])
    if not projects:
        print(f"  {_C.MUTED}No projects in {org}.{_C.RESET}")
        return 0
    for p in projects:
        slug = p.get("slug", "?")
        name = p.get("name", slug)
        pub = "public" if p.get("public") else "private"
        pub_fmt = f"{_C.MUTED}{pub}{_C.RESET}"
        ref = f"{_C.ACCENT}{org}/{slug}{_C.RESET}"
        label = f"  {ref}"
        if name != slug:
            label += f"  {_C.MUTED}{name}{_C.RESET}"
        label += f"  {pub_fmt}"
        print(label)
    return 0


def cmd_projects_create(args) -> int:
    """Create a project and write .stage.toml in cwd."""
    api_key, base_url = _load_credentials()
    ref = args.project
    if "/" not in ref:
        print(f"stage: project must be 'org_slug/project_slug', got {ref!r}", file=sys.stderr)
        return 1
    org_slug, project_slug = ref.split("/", 1)
    body = json.dumps({"slug": project_slug, "name": project_slug, "public": False}).encode()
    data = _bearer_request(f"{base_url}/v1/projects/{org_slug}", api_key, method="POST", body=body)
    created_slug = data.get("project_slug", project_slug)
    print(f"{_C.OK}Created{_C.RESET}  {_C.ACCENT}{org_slug}/{created_slug}{_C.RESET}")
    url = data.get("url", f"{base_url}/{org_slug}/{created_slug}")
    print(_kv("url", url))
    write_project_toml(ref, base_url=base_url)
    print(_kv(".stage.toml", str(Path.cwd() / ".stage.toml")))
    print()
    print(f"  Set the project for this session:")
    print(f"    {_C.BOLD}export ENSEMBLE_STAGE_PROJECT={org_slug}/{created_slug}{_C.RESET}")
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """Push local traces to Stage. Skips runs that already exist there."""
    import glob as _glob
    import uuid as _uuid
    from .stage import Stage, stage_api_call  # noqa: WPS433

    cfg = Stage.resolve()
    if cfg is None:
        print("stage: not configured (set ENSEMBLE_STAGE_API_KEY and ENSEMBLE_STAGE_PROJECT)", file=sys.stderr)
        return 1

    # Collect candidate trace directories from the glob/path argument.
    pattern = args.path
    candidates: List[Path] = []
    p = Path(pattern)
    if p.is_dir():
        # Recursively find all trace.jsonl files under the directory.
        for t in p.rglob("trace.jsonl"):
            candidates.append(t.parent)
    else:
        for match in _glob.glob(pattern, recursive=True):
            mp = Path(match)
            if mp.is_file() and mp.name == "trace.jsonl":
                candidates.append(mp.parent)
            elif mp.is_dir() and (mp / "trace.jsonl").exists():
                candidates.append(mp)

    if not candidates:
        print(f"no trace directories matched {pattern!r}", file=sys.stderr)
        return 1

    pushed = skipped = failed = 0
    for run_dir in sorted(candidates):
        meta_path = run_dir / "meta.json"
        trace_path = run_dir / "trace.jsonl"
        if not trace_path.exists():
            continue
        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass

        local_id = meta.get("run_id") or run_dir.name
        stage_run_id = meta.get("stage_run_id", "").strip()
        short_id = local_id[:32] if len(local_id) > 32 else local_id
        print(f"  {_C.MUTED}{short_id}{_C.RESET}", end="  ", flush=True)

        # A run_id that parses as a UUID is usable as a Stage run ID directly.
        local_is_uuid = _is_uuid(local_id)

        # Skip if already on Stage: prefer stored stage_run_id, fall back to
        # local_id when it is a valid UUID (e.g. Stage-integrated runs).
        check_id = stage_run_id or (local_id if local_is_uuid else None)
        if check_id:
            try:
                stage_api_call(cfg, "GET", f"/v1/runs/{check_id}")
                print(f"{_C.MUTED}skipped{_C.RESET}")
                skipped += 1
                continue
            except RuntimeError:
                pass  # not found; push it

        # Create the run. Pass the local ID when it is a valid UUID so Stage
        # stores the same ID; otherwise let Stage generate a new one.
        create_body: dict = {
            "scenario": meta.get("scenario", ""),
            "world": meta.get("world", ""),
            "backend": meta.get("backend", ""),
            "metadata": {
                **{k: meta[k] for k in ("started_at", "finished_at", "duration_s") if k in meta},
                **({"local_run_id": local_id} if not local_is_uuid else {}),
            },
        }
        if local_is_uuid:
            create_body["id"] = local_id

        try:
            created = stage_api_call(
                cfg, "POST",
                f"/v1/projects/{cfg.org_slug}/{cfg.project_slug}/runs",
                create_body,
            )
        except RuntimeError as e:
            print(f"{_C.ERR}failed{_C.RESET}  {_C.MUTED}create-run: {e}{_C.RESET}")
            failed += 1
            continue

        stage_run_id = created.get("id", "") or local_id
        if meta_path.exists():
            try:
                meta["stage_run_id"] = stage_run_id
                meta["stage_url"] = created.get("url", "")
                meta_path.write_text(json.dumps(meta, indent=2))
            except OSError:
                pass

        # Stream events in batches of 100.
        events_text = trace_path.read_text(errors="replace")
        raw_events = []
        for line in events_text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw_events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

        first_ms = raw_events[0].get("ts_ms", 0) if raw_events else 0
        stage_events = []
        for i, ev in enumerate(raw_events):
            p_payload = ev.get("payload") or {}
            kind = p_payload.get("kind", "system")
            wall_ms = max(0, int((ev.get("ts_ms", first_ms) - first_ms)))
            stage_events.append({
                "sequence_number": i + 1,
                "kind": kind,
                "payload": p_payload,
                "event_id": str(_uuid.uuid4()),
                "wall_time_ms": wall_ms,
            })

        total_ev = len(stage_events)
        ev_errors = 0
        for batch_start in range(0, max(1, total_ev), 100):
            batch = stage_events[batch_start: batch_start + 100]
            if not batch:
                break
            try:
                stage_api_call(cfg, "POST", f"/v1/runs/{stage_run_id}/events", {"events": batch})
            except RuntimeError:
                ev_errors += 1

        # Mark completed.
        scores = meta.get("scores") or {}
        outcome = scores if scores else None
        status_body: dict = {"status": "completed"}
        if outcome:
            status_body["outcome"] = outcome
        costs = meta.get("costs") or {}
        if costs.get("usd"):
            status_body["total_cost"] = {"usd": costs["usd"]}
        if meta.get("duration_s"):
            status_body["wall_time_ms"] = int(meta["duration_s"] * 1000)
        try:
            stage_api_call(cfg, "POST", f"/v1/runs/{stage_run_id}/status", status_body)
        except RuntimeError:
            pass

        ev_ok = total_ev - ev_errors * 100
        if ev_errors:
            print(f"{_C.WARN}pushed{_C.RESET}  {_C.MUTED}{ev_ok}/{total_ev} events{_C.RESET}")
        else:
            print(f"{_C.OK}pushed{_C.RESET}  {_C.MUTED}{total_ev} events{_C.RESET}")
        if stage_run_id:
            stage_url = meta.get("stage_url", "")
            if stage_url:
                print(f"    {_C.MUTED}{stage_url}{_C.RESET}")
        pushed += 1

    ok = f"{_C.OK}{pushed} pushed{_C.RESET}"
    sk = f"{_C.MUTED}{skipped} skipped{_C.RESET}"
    fa = f"{_C.ERR}{failed} failed{_C.RESET}" if failed else f"{_C.MUTED}0 failed{_C.RESET}"
    print(f"\n  {ok}  {sk}  {fa}")
    return 0 if failed == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ensemble.cli_stage")
    sub = parser.add_subparsers(dest="cmd", required=True)

    login_p = sub.add_parser("login", help="Authenticate with Stage (browser OAuth).")
    login_p.add_argument("--base-url", default=PROD_BASE_URL)

    sub.add_parser("logout", help="Remove saved Stage credentials.")
    sub.add_parser("whoami", help="Print the authenticated user's info.")

    projects_p = sub.add_parser("projects", help="Project management.")
    projects_sub = projects_p.add_subparsers(dest="projects_cmd", required=True)
    projects_sub.add_parser("list", help="List accessible projects.")
    create_p = projects_sub.add_parser("create", help="Create a project.")
    create_p.add_argument("project", help="org_slug/project_slug")

    push_p = sub.add_parser("push", help="Bulk-push local traces to Stage.")
    push_p.add_argument(
        "path",
        help="Path to a trace directory, trace.jsonl file, or glob pattern.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "login":
        return cmd_login(args)
    if args.cmd == "logout":
        return cmd_logout(args)
    if args.cmd == "whoami":
        return cmd_whoami(args)
    if args.cmd == "projects":
        if args.projects_cmd == "list":
            return cmd_projects_list(args)
        if args.projects_cmd == "create":
            return cmd_projects_create(args)
    if args.cmd == "push":
        return cmd_push(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
