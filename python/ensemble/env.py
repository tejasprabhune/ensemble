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
    file was found and read, False otherwise."""
    p = Path(path)
    if not p.exists() or not p.is_file():
        return False
    for raw in p.read_text().splitlines():
        key, value = _parse_line(raw)
        if key is None:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
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
