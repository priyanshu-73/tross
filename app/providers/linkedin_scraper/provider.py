"""The credentialed Playwright provider.

Request flow:

    throttle -> ensure session -> load /in/<id>/ -> lazy-load the page
    -> parse the top card + inline sections
    -> (optionally) load each /details/<section>/ page for the *complete* lists
    -> merge and return

The detail pages matter: the main profile page truncates every section (three
roles, three skills, and so on). ``/in/<id>/details/skills/`` returns all of
them, so when a detail page yields more rows than the inline section it wins.

Which sections get a detail pass, the URL templates, the scroll behaviour and
the "profile not found" marker strings all come from ``config.json``.
"""

from __future__ import annotations

import asyncio
import logging
import time

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.config import Settings
from app.errors import (
    AppError,
    ProfileNotAccessible,
    ProfileNotFound,
    ProviderNotConfigured,
    ScrapeFailed,
    UpstreamAuthError,
    UpstreamTimeout,
)
from app.models import LinkedInProfile, ProviderHealth
from app.providers.base import ProfileProvider
from app.providers.linkedin_scraper import parser
from app.providers.linkedin_scraper.browser import BrowserManager
from app.providers.linkedin_scraper.session import LinkedInSession, is_authwalled
from app.site_config import get_site_config

logger = logging.getLogger(__name__)

# config.json section name -> (parse function, LinkedInProfile attribute)
_DETAIL_HANDLERS = {
    "experience": (parser.parse_experience, "experience"),
    "education": (parser.parse_education, "education"),
    "skills": (parser.parse_skills, "skills"),
    "certifications": (parser.parse_certifications, "certifications"),
    "languages": (parser.parse_languages, "languages"),
}


class LinkedInScraperProvider(ProfileProvider):
    name = "linkedin_scraper"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._config = get_site_config()
        self._browser = BrowserManager(settings)
        self._session = LinkedInSession(settings, self._browser)
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)
        self._auth_lock = asyncio.Lock()
        self._throttle_lock = asyncio.Lock()
        self._last_hit = 0.0
        self._startup_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        if not self._settings.has_linkedin_credentials:
            # Boot anyway: /health should be able to *report* the misconfiguration.
            logger.warning("no LinkedIn credentials configured - /profile will return 503")
            return
        try:
            await self._browser.start()
        except AppError as exc:
            # Keep the service up so /health can explain the problem, and record
            # the reason rather than losing it in a startup traceback.
            self._startup_error = exc.message
            logger.error("browser startup failed: %s", exc.message)

    async def shutdown(self) -> None:
        await self._browser.stop()

    async def health(self) -> ProviderHealth:
        configured = self._settings.has_linkedin_credentials and not self._startup_error
        return ProviderHealth(
            name=self.name,
            configured=configured,
            authenticated=self._session.authenticated if configured else None,
            detail=self._startup_error
            or self._session.last_error
            or (None if configured else "LINKEDIN_LI_AT or LINKEDIN_EMAIL/PASSWORD not set"),
        )

    # --- main entry point --------------------------------------------------

    async def fetch_profile(
        self, *, public_id: str, profile_url: str, input_url: str
    ) -> LinkedInProfile:
        if not self._settings.has_linkedin_credentials:
            raise ProviderNotConfigured()

        async with self._semaphore:
            await self._throttle()
            await self._browser.start()

            async with self._auth_lock:
                await self._session.ensure_authenticated()

            try:
                return await self._scrape(public_id, profile_url, input_url)
            except UpstreamAuthError:
                # The session died between the check and the fetch. Re-auth once.
                logger.info("session went stale mid-request; re-authenticating")
                await self._session.invalidate()
                async with self._auth_lock:
                    await self._session.ensure_authenticated(force=True)
                return await self._scrape(public_id, profile_url, input_url)

    # --- internals ---------------------------------------------------------

    async def _throttle(self) -> None:
        """Keep a floor on the gap between LinkedIn page loads."""
        interval = self._settings.scrape_min_interval_seconds
        if interval <= 0:
            return
        async with self._throttle_lock:
            wait = interval - (time.monotonic() - self._last_hit)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit = time.monotonic()

    async def _scrape(self, public_id: str, profile_url: str, input_url: str) -> LinkedInProfile:
        page = await self._browser.new_page()
        try:
            html = await self._load_profile_page(page, profile_url, public_id)
            try:
                profile = parser.parse_profile(
                    html,
                    public_id=public_id,
                    profile_url=profile_url,
                    input_url=input_url,
                    source=self.name,
                )
            except ValueError as exc:
                raise ScrapeFailed(
                    "LinkedIn returned a page this parser did not recognise as a "
                    "profile. This usually means the markup changed or the account "
                    "hit a soft block."
                ) from exc

            if self._settings.fetch_detail_pages:
                await self._merge_detail_pages(page, profile, public_id)
            return profile
        finally:
            await page.close()

    async def _load_profile_page(self, page: Page, profile_url: str, public_id: str) -> str:
        browser_config = self._config.browser
        try:
            response = await page.goto(profile_url, wait_until="domcontentloaded")
        except PlaywrightTimeout as exc:
            raise UpstreamTimeout(f"Timed out loading {profile_url}.") from exc

        if response is not None and response.status == 404:
            raise ProfileNotFound(f"LinkedIn has no profile at '/in/{public_id}'.")

        if is_authwalled(page.url):
            raise UpstreamAuthError(
                "LinkedIn served an authentication wall instead of the profile."
            )

        # The profile heading is the signal that the SPA has hydrated.
        try:
            await page.wait_for_selector(
                browser_config.hydration_selector,
                timeout=browser_config.hydration_timeout_ms,
            )
        except PlaywrightTimeout as exc:
            # The heading never appeared. Work out *why* before reporting.
            body = (await page.content()).lower()
            if any(m in body for m in self._config.detection.not_found_body_markers):
                raise ProfileNotFound(
                    f"LinkedIn has no profile at '/in/{public_id}'."
                ) from exc
            if is_authwalled(page.url):
                raise UpstreamAuthError("LinkedIn served an authentication wall.") from exc
            raise ProfileNotAccessible(
                "The profile did not render for the authenticated account. It may be "
                "out of network, restricted, or the account may be rate-limited."
            ) from exc

        await self._lazy_load(page)
        return await page.content()

    async def _lazy_load(self, page: Page) -> None:
        """Scroll the page so LinkedIn's virtualised sections mount into the DOM."""
        config = self._config.browser
        try:
            for _ in range(config.scroll_steps):
                await page.mouse.wheel(0, config.scroll_delta_px)
                await page.wait_for_timeout(config.scroll_pause_ms)
            await page.evaluate("window.scrollTo(0, 0)")
            await page.wait_for_timeout(self._settings.page_settle_ms)
        except Exception:
            logger.debug("lazy-load scroll failed; parsing what rendered", exc_info=True)

    async def _merge_detail_pages(
        self, page: Page, profile: LinkedInProfile, public_id: str
    ) -> None:
        for section in self._config.linkedin.detail_sections:
            handler = _DETAIL_HANDLERS.get(section)
            if handler is None:
                logger.warning("config.json lists unknown detail section '%s'", section)
                continue
            parse_fn, attribute = handler
            url = self._config.linkedin.details_url(public_id, section)

            try:
                await self._throttle()
                response = await page.goto(url, wait_until="domcontentloaded")
                if response is not None and response.status >= 400:
                    continue
                if is_authwalled(page.url) or "/details/" not in page.url:
                    # Redirected away: the member simply has no such section.
                    continue
                await self._lazy_load(page)
                items = parse_fn(await page.content(), detail_page=True)
            except PlaywrightTimeout:
                logger.info("timed out loading detail page %s; keeping inline data", url)
                continue
            except Exception:
                logger.warning("failed to load detail page %s", url, exc_info=True)
                continue

            # Only replace the inline section when the detail page is richer;
            # a failed parse there must not wipe good data from the main page.
            if len(items) > len(getattr(profile, attribute)):
                setattr(profile, attribute, items)
