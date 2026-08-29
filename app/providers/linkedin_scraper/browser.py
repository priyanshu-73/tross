"""Playwright lifecycle: one browser, one long-lived authenticated context.

Launching Chromium costs ~1s and several hundred MB, so the browser and its
context are created once at startup and reused. Each request gets a fresh
*page* inside the shared context, which keeps cookies (the LinkedIn session)
shared while isolating per-request DOM state.

User agent, viewport, locale and Chromium launch flags all come from
``config.json`` - see `app/site_config.py`.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.config import Settings
from app.errors import IncompatibleEventLoop
from app.site_config import BrowserConfig, get_site_config

logger = logging.getLogger(__name__)

# Chromium reports `navigator.webdriver = true` under automation, which LinkedIn
# treats as an immediate challenge trigger on the login page.
_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
"""


def assert_subprocess_capable_loop() -> None:
    """Fail early, and legibly, if the loop cannot spawn Playwright's driver.

    Playwright launches its Node driver as a subprocess. On Windows only
    ProactorEventLoop supports that; SelectorEventLoop raises a bare
    NotImplementedError from inside asyncio. Checking up front turns an
    inscrutable stack trace into an instruction.
    """
    if sys.platform != "win32":
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:  # not inside a loop; nothing to check
        return
    if isinstance(loop, asyncio.SelectorEventLoop):
        raise IncompatibleEventLoop(
            "Playwright needs a ProactorEventLoop on Windows to launch Chromium, "
            f"but this process is running on {type(loop).__name__}. uvicorn forces "
            "the selector loop whenever it spawns workers, which includes "
            "--reload and --workers. Start the server with 'python run.py' "
            "(keeps hot-reload) or plain 'uvicorn app.main:app' (no --reload). "
            "Linux and macOS are unaffected."
        )


class BrowserManager:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._config: BrowserConfig = get_site_config().browser
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._lock = asyncio.Lock()

    @property
    def state_path(self) -> Path:
        return Path(self._settings.session_state_path)

    @property
    def context(self) -> BrowserContext:
        if self._context is None:
            raise RuntimeError("BrowserManager.start() has not been called")
        return self._context

    async def start(self) -> None:
        async with self._lock:
            if self._context is not None:
                return

            assert_subprocess_capable_loop()

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._settings.headless,
                args=self._config.launch_args,
                proxy=self._settings.playwright_proxy,
            )

            storage_state = None
            if self.state_path.exists() and self.state_path.stat().st_size > 0:
                storage_state = str(self.state_path)
                logger.info("reusing cached LinkedIn session from %s", self.state_path)

            self._context = await self._build_context(storage_state)

    async def stop(self) -> None:
        async with self._lock:
            for closer in (self._context, self._browser):
                if closer is not None:
                    try:
                        await closer.close()
                    except Exception:  # shutdown must not raise
                        logger.debug("error closing browser resource", exc_info=True)
            if self._playwright is not None:
                await self._playwright.stop()
            self._context = self._browser = self._playwright = None

    async def new_page(self) -> Page:
        page = await self.context.new_page()
        blocked = set(self._config.blocked_resource_types)
        if blocked:
            # Images/fonts/media are pure bandwidth here - the image *URLs* we
            # want are in the DOM whether or not the bytes are ever fetched.
            await page.route(
                "**/*",
                lambda route: (
                    route.abort() if route.request.resource_type in blocked else route.continue_()
                ),
            )
        return page

    async def save_state(self) -> None:
        """Persist cookies so a restart does not need to log in again."""
        if self._context is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        await self._context.storage_state(path=str(self.state_path))
        logger.info("saved LinkedIn session state to %s", self.state_path)

    async def reset_context(self) -> None:
        """Drop the current context and its cookies (used after an auth failure)."""
        async with self._lock:
            if self._context is not None:
                try:
                    await self._context.close()
                except Exception:
                    logger.debug("error closing stale context", exc_info=True)
                self._context = None
            if self._browser is not None:
                self._context = await self._build_context(None)

    async def _build_context(self, storage_state: str | None) -> BrowserContext:
        assert self._browser is not None
        context = await self._browser.new_context(
            user_agent=self._config.user_agent,
            viewport=self._config.viewport.model_dump(),
            locale=self._config.locale,
            timezone_id=self._config.timezone_id,
            storage_state=storage_state,
        )
        context.set_default_navigation_timeout(self._settings.nav_timeout_ms)
        context.set_default_timeout(self._settings.nav_timeout_ms)
        await context.add_init_script(_STEALTH_INIT)
        return context
