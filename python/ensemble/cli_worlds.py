"""Entry point for ``ensemble worlds`` subcommands.

The rust CLI shells to ``python -m ensemble.cli_worlds <sub> ...`` so
the registry logic stays in one place and tests can exercise it
without going through the binary.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from .worlds_registry import (
    add_world,
    find_world,
    iter_worlds,
    remove_world,
)
from .world_manifest import ManifestError


def cmd_list(_args: argparse.Namespace) -> int:
    entries = list(iter_worlds())
    if not entries:
        print("(no worlds registered; use `ensemble worlds add <name> <path>`)")
        return 0
    width = max(len(e.name) for e in entries)
    for entry in sorted(entries, key=lambda e: e.name):
        suffix = f"  git={entry.git}" if entry.git else ""
        print(f"{entry.name:<{width}}  {entry.path}{suffix}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    try:
        entry = add_world(args.name, args.path, git=args.git)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except ManifestError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"registered {entry.name} -> {entry.path}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    if remove_world(args.name):
        print(f"removed {args.name}")
        return 0
    print(f"no world named {args.name!r} in registry", file=sys.stderr)
    return 1


def cmd_show(args: argparse.Namespace) -> int:
    entry = find_world(args.name)
    if entry is None:
        print(f"no world named {args.name!r}", file=sys.stderr)
        return 1
    print(f"name: {entry.name}")
    print(f"path: {entry.path}")
    if entry.git:
        print(f"git:  {entry.git}")
    try:
        manifest = entry.manifest()
    except ManifestError as e:
        print(f"manifest error: {e}", file=sys.stderr)
        return 1
    print(f"python_package: {manifest.python_package}")
    if manifest.rust_crate:
        print(f"rust_crate:     {manifest.rust_crate}")
    if manifest.personas_dir:
        print(f"personas_dir:   {manifest.personas_dir}")
    if manifest.default_tools:
        print("default_tools:")
        for t in manifest.default_tools:
            print(f"  - {t}")
    if manifest.default_personas:
        print("default_personas:")
        for p in manifest.default_personas:
            print(f"  - {p}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ensemble.cli_worlds")
    sub = parser.add_subparsers(dest="sub", required=True)

    sub_list = sub.add_parser("list", help="List registered worlds.")
    sub_list.set_defaults(func=cmd_list)

    sub_add = sub.add_parser("add", help="Register a world by local path.")
    sub_add.add_argument("name", help="Short name (matches manifest world.name).")
    sub_add.add_argument("path", type=Path, help="Path to the world directory.")
    sub_add.add_argument("--git", help="Optional git URL (informational).")
    sub_add.set_defaults(func=cmd_add)

    sub_remove = sub.add_parser("remove", help="Unregister a world.")
    sub_remove.add_argument("name")
    sub_remove.set_defaults(func=cmd_remove)

    sub_show = sub.add_parser("show", help="Show a world's manifest details.")
    sub_show.add_argument("name")
    sub_show.set_defaults(func=cmd_show)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
