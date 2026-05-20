"""Plank: a worked example world for Ensemble.

Importing this package registers the plank world with the ensemble
plugin registry. The rust state and tools live in the plank._native
extension; this module wires them up as a ``WorldDefinition`` keyed
by the name ``"plank"``.

A scenario that uses plank should ``import plank`` once near the top
so that ``World("plank")`` finds the registered definition. The
``setup`` factory builds a fresh ``PlankDb`` for each World instance,
keeping per-scenario state isolated.
"""

from __future__ import annotations

from pathlib import Path

from ensemble import (
    PluginPredicate,
    PluginTool,
    register_world,
)

from . import _native

PERSONAS_DIR = Path(__file__).resolve().parent.parent / "personas"


def _tool(db, name, description, parameters, *, timeout_ms=None, resources=None):
    """Bind a plank rust tool to the JSON-string ABI ensemble expects."""

    def fn(args_json: str) -> str:
        return db.dispatch(name, args_json)

    return PluginTool(
        name=name,
        description=description,
        parameters=parameters,
        fn=fn,
        timeout_ms=timeout_ms,
        resources=resources,
    )


def _predicate(db, name):
    def fn(trace_json: str, args_json: str) -> bool:
        try:
            return db.evaluate_predicate(name, trace_json, args_json)
        except KeyError:
            return False

    return PluginPredicate(name=name, fn=fn)


def _setup():
    db = _native.PlankDb()
    tools = [
        _tool(
            db,
            "open_ticket",
            "Open a support ticket on behalf of a user. Required before "
            "any follow-up tool that takes a ticket_id.",
            {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "user_id": {"type": "string"},
                    "subject": {"type": "string"},
                },
                "required": ["ticket_id", "user_id", "subject"],
            },
        ),
        _tool(
            db,
            "lookup_user",
            "Look up a user by id. Returns the user record or null.",
            {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        ),
        _tool(
            db,
            "lookup_ticket",
            "Look up a ticket by id. Returns the ticket record or null.",
            {
                "type": "object",
                "properties": {"ticket_id": {"type": "string"}},
                "required": ["ticket_id"],
            },
        ),
        _tool(
            db,
            "issue_refund",
            "Issue a refund to a user. Amounts are in whole cents. "
            "Acquires the billing_db resource so concurrent refund "
            "attempts serialize.",
            {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "amount_cents": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["user_id", "amount_cents", "reason"],
            },
            resources=["billing_db"],
        ),
        _tool(
            db,
            "escalate",
            "Escalate a ticket to another team. Sets ticket status to 'escalated'.",
            {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string"},
                    "to_team": {"type": "string"},
                },
                "required": ["ticket_id", "to_team"],
            },
        ),
        _tool(
            db,
            "search_kb",
            "Search the knowledge base for relevant articles.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        ),
        _tool(
            db,
            "update_subscription",
            "Move a user to a different plan. Upserts when there is no row yet.",
            {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "plan": {"type": "string"},
                },
                "required": ["user_id", "plan"],
            },
        ),
        _tool(
            db,
            "slow_billing_check",
            "Slow billing reconciliation. Emits progress while it works.",
            {
                "type": "object",
                "properties": {
                    "user_id": {"type": "string"},
                    "steps": {"type": "integer"},
                },
                "required": ["user_id"],
            },
        ),
    ]
    predicates = [_predicate(db, name) for name in db.predicate_names()]
    return tools, predicates


register_world("plank", setup=_setup, personas_dir=PERSONAS_DIR)


__all__ = ["PERSONAS_DIR"]
