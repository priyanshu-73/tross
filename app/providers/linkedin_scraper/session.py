"""Establishing and validating the authenticated LinkedIn session.

Two ways in, in order of preference:

1. **`li_at` cookie** (``LINKEDIN_LI_AT``). Injected straight into the browser
   context. Nothing about the request looks like a login, so LinkedIn's
   challenge machinery is never invoked. This is what should be used in
   production.
2. **Email + password** (``LINKEDIN_EMAIL`` / ``LINKEDIN_PASSWORD``). Drives the
   real login form. Works, but a login from a datacentre IP is exactly the
   signal LinkedIn escalates on, so expect email verification / CAPTCHA
   challenges - which this module detects and reports rather than trying to
   defeat.

URLs, form selectors and the marker strings used to recognise an auth wall all
live in ``config.json``.
"""

from __future__ import annotations

import logging

from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.config import Settings
from app.errors import ProviderNotConfigured, UpstreamAuthError, UpstreamTimeout
from app.providers.linkedin_scraper.browser import BrowserManager
from app.site_config import get_site_config

logger = logging.getLogger(__name__)


def is_authwalled(url: str) -> bool:
    """True when LinkedIn has bounced us to a sign-in / verification page."""
    lowered = (url or "").lower()
    return any(m in lowered for m in get_site_config().detection.authwall_url_markers)


def is_challenge(url: str) -> bool:
    """True for the 2FA / CAPTCHA / device-verification interstitials."""
    lowered = (url or "").lower()
    return any(m in lowered for m in get_site_config().detection.challenge_url_markers)


class LinkedInSession:
    """Owns the 'are we logged in?' question for a `BrowserManager`."""

    def __init__(self, settings: Settings, browser: BrowserManager) -> None:
        self._settings = settings
        self._browser = browser
        self._config = get_site_config()
        self.authenticated = False
        self.last_error: str | None = None

    async def ensure_authenticated(self, *, force: bool = False) -> None:
        if self.authenticated and not force:
            return
        if not self._settings.has_linkedin_credentials:
            raise ProviderNotConfigured(
                "No LinkedIn credentials configured. Set LINKEDIN_LI_AT (preferred) "
                "or LINKEDIN_EMAIL + LINKEDIN_PASSWORD."
            )

        await self._inject_cookie()

        page = await self._browser.new_page()
        try:
            if await self._session_is_live(page):
                self.authenticated = True
                self.last_error = None
                await self._browser.save_state()
                return

            if not (self._settings.linkedin_email and self._settings.linkedin_password):
                self.authenticated = False
                self.last_error = "li_at cookie rejected by LinkedIn (expired or invalid)"
                raise UpstreamAuthError(
                    "The configured LINKEDIN_LI_AT cookie is no longer valid. "
                    "Refresh it from a signed-in browser, or configure "
                    "LINKEDIN_EMAIL / LINKEDIN_PASSWORD."
                )

            await self._login_with_password(page)
            self.authenticated = True
            self.last_error = None
            await self._browser.save_state()
        finally:
            await page.close()

    async def invalidate(self) -> None:
        """Called when a request discovers the session died mid-flight."""
        self.authenticated = False

    # --- internals ---------------------------------------------------------

    async def _inject_cookie(self) -> None:
        secret = self._settings.linkedin_li_at
        if not secret:
            return
        value = secret.get_secret_value().strip()
        if not value:
            return
        await self._browser.context.add_cookies(
            [
                {
                    "name": "li_at",
                    "value": value,
                    "domain": ".linkedin.com",
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "None",
                }
            ]
        )

    async def _session_is_live(self, page: Page) -> bool:
        try:
            await page.goto(self._config.linkedin.feed_url, wait_until="domcontentloaded")
        except PlaywrightTimeout as exc:
            raise UpstreamTimeout("Timed out contacting LinkedIn.") from exc
        return not is_authwalled(page.url)

    async def _login_with_password(self, page: Page) -> None:
        email = self._settings.linkedin_email or ""
        password = (
            self._settings.linkedin_password.get_secret_value()
            if self._settings.linkedin_password
            else ""
        )
        selectors = self._config.login_selectors

        logger.info("performing LinkedIn form login for %s", _mask(email))
        try:
            await page.goto(self._config.linkedin.login_url, wait_until="domcontentloaded")
            await page.fill(selectors.username, email)
            await page.fill(selectors.password, password)
            await page.click(selectors.submit)
            await page.wait_for_load_state("domcontentloaded")
            await page.wait_for_timeout(self._settings.page_settle_ms)
        except PlaywrightTimeout as exc:
            raise UpstreamTimeout("Timed out during LinkedIn login.") from exc

        url = page.url
        if is_challenge(url):
            self.last_error = "security checkpoint (2FA / CAPTCHA / device verification)"
            raise UpstreamAuthError(
                "LinkedIn presented a security checkpoint (2FA, CAPTCHA, or device "
                "verification) that cannot be solved headlessly. Sign in to this "
                "account manually from a browser, then supply the resulting li_at "
                "cookie via LINKEDIN_LI_AT."
            )

        error_node = await page.query_selector(selectors.error)
        if error_node is not None:
            detail = (await error_node.inner_text()).strip()
            if detail:
                self.last_error = detail
                raise UpstreamAuthError(f"LinkedIn rejected the credentials: {detail}")

        if is_authwalled(url):
            self.last_error = "login did not produce an authenticated session"
            raise UpstreamAuthError(
                "Login did not produce an authenticated session (LinkedIn kept us "
                "on the sign-in wall)."
            )


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{name[:2]}***@{domain}"
