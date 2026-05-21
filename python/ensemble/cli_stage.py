"""Stage CLI subcommands: login, logout, whoami, projects."""

from __future__ import annotations

import argparse
import json
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
        print(f"stage: HTTP {e.code}: {body_text}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"stage: network error: {e.reason}", file=sys.stderr)
        sys.exit(1)


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
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Logged in! You can close this tab.</h1>")
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
        print("stage: login timed out or was cancelled.", file=sys.stderr)
        return 1

    user_login = ""
    try:
        data = _bearer_request(f"{base_url}/v1/me", api_key)
        user_login = data.get("github_login", "")
    except SystemExit:
        pass

    write_credentials(api_key, base_url=base_url, user_login=user_login)
    print(f"Logged in as {user_login or '(unknown)'}. Credentials saved to {_CREDS_PATH}.")
    return 0


def cmd_logout(_args) -> int:
    """Remove credentials from ~/.ensemble/stage.toml."""
    if _CREDS_PATH.exists():
        _CREDS_PATH.unlink()
        print(f"Removed {_CREDS_PATH}.")
    else:
        print("Not logged in.")
    return 0


def cmd_whoami(_args) -> int:
    """Call GET /v1/me and print user info."""
    api_key, base_url = _load_credentials()
    data = _bearer_request(f"{base_url}/v1/me", api_key)
    login = data.get("github_login", "")
    email = data.get("email", "")
    org = data.get("default_org_slug", "")
    print(f"Logged in as: {login}")
    if email:
        print(f"Email:        {email}")
    if org:
        print(f"Default org:  {org}")
    return 0


def cmd_projects_list(_args) -> int:
    """List accessible projects."""
    api_key, base_url = _load_credentials()
    me = _bearer_request(f"{base_url}/v1/me", api_key)
    org = me.get("default_org_slug", "")
    if not org:
        print("stage: no default org found. Use 'ensemble stage whoami' to debug.", file=sys.stderr)
        return 1
    data = _bearer_request(f"{base_url}/v1/projects/{org}", api_key)
    projects = data if isinstance(data, list) else data.get("projects", [])
    if not projects:
        print(f"No projects in {org}.")
    for p in projects:
        slug = p.get("slug", p.get("name", "?"))
        print(f"  {org}/{slug}")
    return 0


def cmd_projects_create(args) -> int:
    """Create a project and write .stage.toml in cwd."""
    api_key, base_url = _load_credentials()
    ref = args.project
    if "/" not in ref:
        print(f"stage: project must be 'org_slug/project_slug', got {ref!r}", file=sys.stderr)
        return 1
    org_slug, project_slug = ref.split("/", 1)
    body = json.dumps({"slug": project_slug}).encode()
    data = _bearer_request(f"{base_url}/v1/projects/{org_slug}", api_key, method="POST", body=body)
    print(f"Created project: {org_slug}/{data.get('slug', project_slug)}")
    write_project_toml(ref, base_url=base_url)
    print(f"Wrote .stage.toml in {Path.cwd()}")
    return 0


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
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
