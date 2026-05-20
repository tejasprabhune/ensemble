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
    # Only mark items in this conftest's directory; otherwise this
    # hook would also skip the unit / mock-integration suite when
    # pytest collects tests/ as a whole.
    here = str(__import__("pathlib").Path(__file__).parent.resolve())
    for item in items:
        try:
            path = str(item.path.resolve())
        except AttributeError:
            path = str(item.fspath)
        if path.startswith(here):
            item.add_marker(skip)


@pytest.fixture
def have_anthropic():
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")


@pytest.fixture
def have_openai():
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY not set")
