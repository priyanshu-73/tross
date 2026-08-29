"""Fetch the profile page, read the JSON LinkedIn embedded in it.

One GET, no browser, no API endpoint to guess:

    GET /in/<public-id>/   ->  HTML  ->  embedded JSON  ->  models

This is the durable version of the direct-API idea. The Voyager provider had to
name an endpoint (``/identity/profiles/{id}/profileView``), and LinkedIn retired
it — HTTP 410, no notice, no replacement announced. The profile URL is the one
address that cannot be retired, and whatever data the page needs is inlined in
the response, so this transport follows LinkedIn's own changes for free.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from app.config import Settings
from app.errors import (
    AppError,
    ProfileNotAccessible,
    ProfileNotFound,
    ProviderNotConfigured,
    RateLimited,
    ScrapeFailed,
    UpstreamAuthError,
    UpstreamTimeout,
)
from app.models import LinkedInProfile, ProviderHealth
from app.providers.base import ProfileProvider
from app.providers.embedded import extractor, mapper
from app.providers.session import LinkedInAuthenticator, is_session_revoked
from app.site_config import get_site_config

logger = logging.getLogger(__name__)

HTTP_BLOCKED = 999


class EmbeddedProvider(ProfileProvider):
    name = "embedded"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        site = get_site_config()
        self._config = site.embedded
        self._vocab = site.vocabulary
        self._user_agent = site.browser.user_agent
        self._auth = LinkedInAuthenticator(settings, site.auth, self._user_agent)
        self._home_url = site.linkedin.base_url
        self._warmed = False
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)
        self._throttle_lock = asyncio.Lock()
        self._last_hit = 0.0
        self._startup_error: str | None = None
        self.last_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        if not self._auth.configured:
            logger.warning(
                "no LinkedIn session source configured - /profile will return 503"
            )
            return
        self._client = httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
            follow_redirects=True,  # locale subdomains legitimately redirect
            headers={"user-agent": self._user_agent, **self._config.headers},
        )
        try:
            # Installs a stored or configured session. Only logs in if there is
            # nothing to reuse - boot should not cost a sign-in.
            await self._auth.apply(self._client)
            self._warmed = False
        except AppError as exc:
            self._startup_error = exc.message
            logger.error("could not establish a LinkedIn session: %s", exc.message)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health(self) -> ProviderHealth:
        configured = self._auth.configured and not self._startup_error
        return ProviderHealth(
            name=self.name,
            configured=configured,
            authenticated=bool(self._client and self._auth.source) if configured else None,
            detail=self._startup_error
            or self.last_error
            or self._auth.last_error
            or (
                f"session source: {self._auth.source}"
                if configured
                else "set LINKEDIN_EMAIL + LINKEDIN_PASSWORD, or LINKEDIN_LI_AT"
            ),
        )

    # --- main entry point --------------------------------------------------

    async def fetch_profile(
        self, *, public_id: str, profile_url: str, input_url: str
    ) -> LinkedInProfile:
        if not self._auth.configured:
            raise ProviderNotConfigured(
                "No LinkedIn session source. Set LINKEDIN_EMAIL + LINKEDIN_PASSWORD "
                "so the service can sign in itself, or supply LINKEDIN_LI_AT."
            )
        if self._client is None:
            await self.startup()
        assert self._client is not None

        async with self._semaphore:
            await self._throttle()
            self._require_session()
            try:
                await self._warm_up()
                html = await self._get_page(public_id)
            except UpstreamAuthError:
                # The session died. Mint a new one and try exactly once more -
                # a second failure is a real problem, not an expiry.
                if not self._auth.can_login:
                    raise
                logger.info("session rejected; re-authenticating and retrying once")
                await self._auth.refresh(self._client)
                self._startup_error = None
                self._warmed = False
                self._require_session()
                await self._warm_up()
                html = await self._get_page(public_id)

        return self._parse(html, public_id, profile_url, input_url)

    def _require_session(self) -> None:
        """Refuse to serve anonymous data under this provider's name.

        Without a session LinkedIn still returns *a* page — the logged-out one,
        carrying only the public JSON-LD. Parsing that and labelling it
        `source: embedded` would quietly hand back a thin profile that looks
        like a rich one, which is worse than an error. If anonymous data is
        wanted, `PROVIDER=public` says so honestly.
        """
        if self._auth.source is None:
            raise UpstreamAuthError(
                "No LinkedIn session is established, and this provider will not "
                "fall back to anonymous data under its own name. Mint a session "
                "with 'python scripts/login.py', or set PROVIDER=public to use "
                "the credential-free provider deliberately."
            )

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

    async def _warm_up(self) -> None:
        """Touch the homepage so LinkedIn tops up its own cookies.

        A stored `li_at` is necessary but not sufficient: LinkedIn also wants the
        short-lived cookies it hands out per visit (`lidc`, `JSESSIONID`). Without
        them a profile request bounces between interstitials until httpx gives up
        with TooManyRedirects. One cheap GET makes the session whole.
        """
        assert self._client is not None
        if self._warmed:
            return
        try:
            # No redirect-following here: the revocation signal rides on the 302,
            # and following it would bury the one header that explains the failure.
            response = await self._client.get(self._home_url, follow_redirects=False)
        except httpx.HTTPError:
            # The profile request will report the real problem.
            logger.warning("could not warm up the LinkedIn session", exc_info=True)
            return

        if is_session_revoked(response):
            self.last_error = "LinkedIn revoked the session cookie"
            raise UpstreamAuthError(
                "LinkedIn has revoked this session - it answered with "
                "'set-cookie: li_at=delete me'. The cookie is dead and no retry "
                "will revive it. Mint a new one with 'python scripts/login.py'."
            )
        self._warmed = True

    async def _get_page(self, public_id: str) -> str:
        assert self._client is not None
        url = self._config.profile_url(public_id)

        try:
            response = await self._client.get(url, headers={"referer": self._home_url})
        except httpx.TooManyRedirects as exc:
            # LinkedIn ping-pongs a half-valid session rather than rejecting it.
            self._warmed = False
            raise UpstreamAuthError(
                "LinkedIn redirect-looped the request, which means the session was "
                "accepted but is incomplete or stale. Re-mint it with "
                "'python scripts/login.py'."
            ) from exc
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(f"Timed out loading {url}.") from exc
        except httpx.HTTPError as exc:
            raise UpstreamAuthError(f"Could not reach LinkedIn: {exc}") from exc

        status = response.status_code
        logger.info(
            "fetched %s -> HTTP %s, %d bytes, final URL %s",
            url, status, len(response.content), response.url,
        )
        if status == 404:
            # Usually literal, but LinkedIn also 404s pages it declines to serve,
            # so the message names both readings rather than asserting one.
            raise ProfileNotFound(
                f"LinkedIn returned 404 for '/in/{public_id}'. Either no profile "
                "exists at that identifier (check the slug — most have a suffix, "
                "e.g. 'jane-doe-1a2b3c'), or LinkedIn declined to serve it to this "
                "session. 'python scripts/probe.py <url>' distinguishes the two."
            )
        if status == 429:
            raise RateLimited("LinkedIn is rate-limiting this account. Back off and retry.")
        if status == HTTP_BLOCKED:
            self.last_error = "LinkedIn returned 999 (request blocked)"
            raise UpstreamAuthError(
                "LinkedIn returned HTTP 999, meaning it classified this request as "
                "automated. The IP is likely flagged; a residential proxy and a "
                "slower request rate are the usual remedies."
            )
        if status in (401, 403):
            self.last_error = f"LinkedIn returned HTTP {status}"
            raise UpstreamAuthError(
                f"LinkedIn rejected the request (HTTP {status}). The li_at cookie is "
                "expired, or this profile is not visible to the account."
            )
        if status >= 400:
            raise UpstreamAuthError(f"LinkedIn returned an unexpected HTTP {status}.")

        # follow_redirects means an authwall arrives as a 200 at a different URL.
        final_url = str(response.url).lower()
        if any(marker in final_url for marker in ("/authwall", "/login", "/signup")):
            self.last_error = "redirected to the sign-in wall"
            raise UpstreamAuthError(
                "LinkedIn redirected to its sign-in wall, so the li_at cookie is no "
                "longer valid. Refresh it with 'python scripts/login.py'."
            )

        self.last_error = None
        return response.text

    def _parse(
        self, html: str, public_id: str, profile_url: str, input_url: str
    ) -> LinkedInProfile:
        blobs = extractor.iter_json_blobs(html)
        entities = extractor.collect_included(blobs)
        logger.info(
            "embedded extraction: %d blobs, %d entities for %s",
            len(blobs),
            len(entities),
            public_id,
        )

        try:
            return mapper.map_profile(
                entities,
                blobs,
                public_id=public_id,
                profile_url=profile_url,
                input_url=input_url,
                config=self._config,
                vocab=self._vocab,
                source=self.name,
            )
        except ValueError as exc:
            self._raise_for_empty_page(html, public_id, exc)
            raise  # unreachable; _raise_for_empty_page always raises

    def _raise_for_empty_page(self, html: str, public_id: str, exc: ValueError) -> None:
        """Work out *why* the page carried no profile data before reporting."""
        lowered = html.lower()

        if any(marker in lowered for marker in self._config.authwall_markers):
            raise UpstreamAuthError(
                "LinkedIn served a sign-in wall instead of the profile — the session "
                "cookie is not being accepted. Refresh it with "
                "'python scripts/login.py'."
            ) from exc

        if any(
            marker in lowered
            for marker in get_site_config().detection.not_found_body_markers
        ):
            raise ProfileNotFound(f"LinkedIn has no profile at '/in/{public_id}'.") from exc

        if len(html) < 5_000:
            raise ProfileNotAccessible(
                "LinkedIn returned a near-empty page, which usually means the profile "
                "is restricted or the account is being throttled."
            ) from exc

        raise ScrapeFailed(
            "The profile page loaded but carried no recognisable embedded JSON "
            f"({exc}). LinkedIn most likely changed how it inlines page data; the "
            "entity type names are in config.json under 'embedded.entity_types'."
        ) from exc
