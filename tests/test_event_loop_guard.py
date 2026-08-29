"""Regression tests for the Windows event-loop guard.

Playwright launches its Node driver as a subprocess, which on Windows only
ProactorEventLoop supports. uvicorn swaps in SelectorEventLoop whenever it
spawns workers (`--reload`, `--workers N`), and the raw symptom is a bare
`NotImplementedError` from inside asyncio that names neither the cause nor the
fix. These tests pin the guard that turns it into an instruction.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from app.errors import IncompatibleEventLoop
from app.providers.linkedin_scraper.browser import assert_subprocess_capable_loop


def test_a_subprocess_capable_loop_is_accepted():
    """The platform default (Proactor on Windows, any loop on POSIX) passes."""

    async def check() -> None:
        assert_subprocess_capable_loop()

    asyncio.run(check())


def test_outside_a_running_loop_the_guard_is_a_no_op():
    assert_subprocess_capable_loop()


@pytest.mark.skipif(sys.platform != "win32", reason="constraint is Windows-only")
def test_selector_loop_is_rejected_with_an_actionable_message():
    async def check() -> None:
        assert_subprocess_capable_loop()

    loop = asyncio.SelectorEventLoop()
    try:
        with pytest.raises(IncompatibleEventLoop) as excinfo:
            loop.run_until_complete(check())
    finally:
        loop.close()

    message = str(excinfo.value)
    # The message has to name the fix, not just the problem.
    assert "run.py" in message
    assert "--reload" in message
    assert excinfo.value.code == "incompatible_event_loop"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX selector loops spawn fine")
def test_selector_loop_is_fine_on_posix():
    async def check() -> None:
        assert_subprocess_capable_loop()

    loop = asyncio.SelectorEventLoop()
    try:
        loop.run_until_complete(check())  # must not raise
    finally:
        loop.close()
