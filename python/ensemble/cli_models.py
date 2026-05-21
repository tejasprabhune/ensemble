"""Entry point used by `ensemble models list`.

Reports the shipped backends, whether their environment-variable
keys are set, and the model identifiers each backend knows about
(from the pricing table that ships with the runtime crate). Lets a
researcher answer "what can I pass as model='...'" without grepping
the source.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:
    import tomli as _toml  # type: ignore[import-not-found]


_PACKAGE_ROOT = Path(__file__).resolve().parent
_REPO_ROOT_GUESS = _PACKAGE_ROOT.parent.parent
_PRICING_PATH = _REPO_ROOT_GUESS / "crates/ensemble-runtime/pricing.toml"


def _load_pricing_table() -> Dict[str, Dict[str, Dict[str, float]]]:
    """Best-effort: read the pricing TOML the runtime crate uses so
    the model list stays in sync with the canonical source. Returns
    an empty table if the file is missing (the python package can be
    installed without the crate source tree alongside it)."""
    if not _PRICING_PATH.exists():
        return {"anthropic": {}, "openai": {}}
    try:
        data = _toml.loads(_PRICING_PATH.read_text())
    except _toml.TOMLDecodeError as e:
        print(f"warning: pricing table at {_PRICING_PATH} is invalid: {e}", file=sys.stderr)
        return {"anthropic": {}, "openai": {}}
    return data


def _key_status(env_var: str) -> Tuple[bool, str]:
    raw = os.environ.get(env_var, "")
    if not raw:
        return False, f"  {env_var}: not set"
    return True, f"  {env_var}: set ({raw[:6]}...)"


def _format_backend(
    name: str,
    label: str,
    key_var: Optional[str],
    models: List[str],
    extra: Optional[str] = None,
) -> str:
    lines = [f"[{name}] {label}"]
    if key_var is not None:
        _, status = _key_status(key_var)
        lines.append(status)
    if extra is not None:
        lines.append(f"  {extra}")
    if models:
        lines.append("  models:")
        for m in sorted(models):
            lines.append(f"    - {m}")
    return "\n".join(lines)


def list_models(_args: argparse.Namespace) -> int:
    pricing = _load_pricing_table()
    anthropic_models = list(pricing.get("anthropic", {}).keys())
    openai_models = list(pricing.get("openai", {}).keys())

    sections = [
        _format_backend(
            "anthropic",
            "Anthropic Claude family (production).",
            "ANTHROPIC_API_KEY",
            anthropic_models,
        ),
        _format_backend(
            "openai",
            "OpenAI chat completions (production).",
            "OPENAI_API_KEY",
            openai_models,
        ),
        _format_backend(
            "vllm",
            "Local or remote vLLM server (typically used for trained personas).",
            None,
            [],
            extra=(
                f"ENSEMBLE_VLLM_BASE_URL: "
                f"{os.environ.get('ENSEMBLE_VLLM_BASE_URL') or 'not set'} "
                "(model names depend on what the endpoint serves)"
            ),
        ),
        _format_backend(
            "mock",
            "Deterministic stub backend; produces canned replies for tests and demos.",
            None,
            [],
            extra="no key required; pass backend='mock' to force, or fall through automatically.",
        ),
    ]

    print("\n\n".join(sections))
    print("\nTo pick a backend explicitly: pass --backend <name> to ensemble run.")
    print("To auto-select from your env: pass --backend auto.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ensemble.cli_models",
        description="Inspect the LLM backends ensemble knows about.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="Print backends, key status, and models.")
    p_list.set_defaults(func=list_models)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
