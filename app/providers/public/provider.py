"""Public profile provider — no LinkedIn account, no cookie, no ban risk.

Reads the schema.org JSON-LD that LinkedIn publishes in public profile pages for
search engines. Nothing here is authenticated, so there is no session to expire,
no credential to rotate, and no account that can be restricted.

The one thing it needs is a **guest session**. Requesting a profile URL cold gets
HTTP 999 — LinkedIn's "this looks automated" response — because a real visitor
never arrives without the cookies its homepage sets. Landing on the homepage
first, exactly as a browser would, and carrying those cookies forward is enough:
the same profile that returns 999 cold returns 200 warm.

Coverage is the trade. Members can hide their profile from logged-out visitors,
and LinkedIn declines some requests outright; both surface as
`profile_not_accessible` rather than pretending. When it does work you get name,
headline, location, about, photo, employers with roles and dates, schools,
languages and a follower count — but never skills, certifications, connection
counts, or per-role descriptions, which exist only behind a session.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.config import Settings
from app.errors import (
    ProfileNotAccessible,
    ProfileNotFound,
    RateLimited,
    ScrapeFailed,
    UpstreamTimeout,
)
from app.models import LinkedInProfile, ProviderHealth
from app.providers.base import ProfileProvider
from app.providers.embedded import extractor
from app.providers.public import mapper
from app.site_config import get_site_config

logger = logging.getLogger(__name__)

HTTP_BLOCKED = 999


class PublicProvider(ProfileProvider):
    name = "public"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        site = get_site_config()
        self._config = site.embedded
        self._home_url = site.linkedin.base_url
        self._user_agent = site.browser.user_agent
        self._client: httpx.AsyncClient | None = None
        self._guest_session = False
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)
        self._throttle_lock = asyncio.Lock()
        self._last_hit = 0.0

    # --- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
            follow_redirects=True,
            headers={"user-agent": self._user_agent, **self._config.headers},
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health(self) -> ProviderHealth:
        # Needs no credentials, so it is always configured.
        return ProviderHealth(
            name=self.name,
            configured=True,
            authenticated=False,
            detail="anonymous - public profile data only, no credentials required",
        )

    # --- main entry point --------------------------------------------------

    async def fetch_profile(
        self, *, public_id: str, profile_url: str, input_url: str
    ) -> LinkedInProfile:
        if self._client is None:
            await self.startup()
        assert self._client is not None

        async with self._semaphore:
            await self._throttle()
            await self._ensure_guest_session()
            html = await self._get_page(public_id)

        person = extractor.find_json_ld_person(extractor.iter_json_blobs(html))
        if person is None:
            raise ProfileNotAccessible(
                "The page carried no public profile record. LinkedIn publishes one "
                "only for profiles visible to logged-out visitors, so this member "
                "has most likely restricted theirs. An authenticated provider "
                "(PROVIDER=embedded) can still read it."
            )

        try:
            return mapper.map_profile(
                person,
                public_id=public_id,
                profile_url=profile_url,
                input_url=input_url,
                source=self.name,
            )
        except ValueError as exc:
            raise ScrapeFailed(
                f"LinkedIn's public profile record could not be mapped ({exc})."
            ) from exc

    # --- internals ---------------------------------------------------------

    async def _throttle(self) -> None:
        interval = self._settings.scrape_min_interval_seconds
        if interval <= 0:
            return
        async with self._throttle_lock:
            wait = interval - (time.monotonic() - self._last_hit)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit = time.monotonic()

    async def _ensure_guest_session(self) -> None:
        """Collect the cookies LinkedIn's homepage issues to every visitor.

        Without them a profile request is answered with HTTP 999. With them the
        same request succeeds — this is the difference between arriving like a
        browser and arriving like a script.
        """
        assert self._client is not None
        if self._guest_session:
            return
        try:
            await self._client.get(self._home_url)
            self._guest_session = True
            logger.info("guest session established (%d cookies)", len(self._client.cookies))
        except httpx.HTTPError:
            # Not fatal on its own; the profile request will report the real error.
            logger.warning("could not establish a guest session", exc_info=True)

    async def _get_page(self, public_id: str) -> str:
        assert self._client is not None
        url = self._config.profile_url(public_id)

        try:
            response = await self._client.get(url, headers={"referer": self._home_url})
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(f"Timed out loading {url}.") from exc
        except httpx.HTTPError as exc:
            raise ScrapeFailed(f"Could not reach LinkedIn: {exc}") from exc

        status = response.status_code
        logger.info(
            "public fetch %s -> HTTP %s, %d bytes", url, status, len(response.content)
        )

        if status == 404:
            raise ProfileNotFound(f"LinkedIn has no profile at '/in/{public_id}'.")
        if status == 429:
            raise RateLimited("LinkedIn is rate-limiting anonymous requests. Back off.")
        if status == HTTP_BLOCKED:
            # Retried once with a fresh guest session before giving up, since a
            # stale or missing guest cookie is the usual cause.
            self._guest_session = False
            raise ProfileNotAccessible(
                "LinkedIn declined to serve this profile anonymously (HTTP 999). "
                "Either the member hides it from logged-out visitors, or this IP is "
                "being throttled. An authenticated provider can still read it."
            )
        if status >= 400:
            raise ScrapeFailed(f"LinkedIn returned HTTP {status} for the public page.")

        final = str(response.url).lower()
        if "/authwall" in final or "/login" in final:
            raise ProfileNotAccessible(
                "LinkedIn redirected to its sign-in wall, so this profile is not "
                "publicly visible."
            )

        return response.text
