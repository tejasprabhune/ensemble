"""Tiny dotenv loader. No external dependency.

The format is the subset most projects actually use: one
``KEY=value`` per line, ``#`` comments, optional ``export`` prefix,
and optional single or double quoted values. Variables already set
in ``os.environ`` are left alone.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def load_dotenv(path: str | Path = ".env", override: bool = False) -> bool:
    """Load environment variables from a dotenv file. Returns True if a
    file was found and read, False otherwise.

    When `override=False` (the default), variables already set in
    `os.environ` win over the file. If the file tries to set a key
    that the shell already exported with a different value, we print
    a warning on stderr so the conflict is visible. Pass
    `override=True` to make the file always win.
    """
    import sys

    p = Path(path)
    if not p.exists() or not p.is_file():
        return False
    for raw in p.read_text().splitlines():
        key, value = _parse_line(raw)
        if key is None:
            continue
        existing = os.environ.get(key)
        if override or existing is None:
            os.environ[key] = value
        elif existing != value and key.endswith(("_API_KEY", "_BASE_URL", "_TOKEN")):
            short_env = (existing[:6] + "...") if len(existing) > 6 else existing
            short_file = (value[:6] + "...") if len(value) > 6 else value
            print(
                f"ensemble: warning, {key} in shell env ({short_env}) "
                f"overrides {path} ({short_file}); pass dotenv-override or "
                f"unset {key} in your shell to use the .env value.",
                file=sys.stderr,
            )
    return True


def _parse_line(raw: str) -> tuple[Optional[str], str]:
    line = raw.strip()
    if not line or line.startswith("#"):
        return None, ""
    if line.startswith("export "):
        line = line[len("export "):].lstrip()
    if "=" not in line:
        return None, ""
    key, _, value = line.partition("=")
    key = key.strip()
    if not key.replace("_", "").isalnum():
        return None, ""
    value = _strip_inline_comment(value.strip())
    value = _unquote(value)
    return key, value


def _strip_inline_comment(value: str) -> str:
    if value.startswith(('"', "'")):
        return value
    idx = value.find(" #")
    if idx >= 0:
        return value[:idx].rstrip()
    return value


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        return value[1:-1]
    return value
