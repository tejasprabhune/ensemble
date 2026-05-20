"""Shared fixtures for the live-API suite.

Every test in this directory is skipped unless ``LIVE_API_TESTS=1``
is set in the environment. The suite hits real Anthropic and OpenAI
endpoints; expect roughly $0.10 to $1.00 per full run depending on
how the providers happen to bill that day.
"""

from __future__ import annotations

import os

import pytest


LIVE_GATE = os.environ.get("LIVE_API_TESTS") == "1"


def pytest_collection_modifyitems(config, items):
    if LIVE_GATE:
        return
    skip = pytest.mark.skip(
        reason="set LIVE_API_TESTS=1 to run the live-API suite",
    )
    for item in items:
        item.add_marker(skip)


@pytest.fixture
def have_anthropic():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")


@pytest.fixture
def have_openai():
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")
