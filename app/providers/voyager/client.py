"""HTTP client for LinkedIn's Voyager Dash API.

Voyager is the JSON API linkedin.com's own client calls. Hitting it directly is
the fastest and cleanest transport available: one authenticated GET returns the
whole profile as structured entities — no browser, no HTML, no lazy loading.

Two things it needs:

* **The session cookie jar**, obtained by `LinkedInAuthenticator`. `li_at` alone
  is not enough; LinkedIn redirect-loops a request missing the cookies it issued
  alongside it.
* **A CSRF token**, which is just the `JSESSIONID` cookie value echoed back in a
  `csrf-token` header. That is LinkedIn's convention, not a secret.

Endpoint paths are configuration, not constants, because LinkedIn retires them
without notice — the previous generation now answers 410.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings
from app.errors import (
    ProfileNotFound,
    ProviderNotConfigured,
    RateLimited,
    UpstreamAuthError,
    UpstreamEndpointGone,
    UpstreamTimeout,
)
from app.providers.session import LinkedInAuthenticator, is_session_revoked
from app.site_config import VoyagerConfig

logger = logging.getLogger(__name__)

HTTP_BLOCKED = 999
HTTP_GONE = 410


class VoyagerClient:
    """Authenticated access to the Dash endpoints."""

    def __init__(
        self,
        settings: Settings,
        config: VoyagerConfig,
        auth: LinkedInAuthenticator,
        user_agent: str,
    ) -> None:
        self._settings = settings
        self._config = config
        self._auth = auth
        self._user_agent = user_agent
        self._client: httpx.AsyncClient | None = None
        self._warmed = False
        self.last_error: str | None = None

    @property
    def authenticated(self) -> bool:
        return self._client is not None and self._auth.source is not None

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        if self._client is not None:
            return
        if not self._auth.configured:
            raise ProviderNotConfigured(
                "No LinkedIn session available. Run 'python scripts/login.py' to "
                "mint one, or set LINKEDIN_LI_AT."
            )
        self._client = httpx.AsyncClient(
            timeout=self._config.timeout_seconds,
            follow_redirects=False,  # a redirect here is an auth failure, not a hop
            headers={"user-agent": self._user_agent, **self._config.headers},
        )
        await self._auth.apply(self._client)
        self._warmed = False

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._warmed = False

    async def reauthenticate(self) -> None:
        if self._client is None:
            await self.start()
            return
        await self._auth.refresh(self._client)
        self._warmed = False

    # --- requests ----------------------------------------------------------

    @property
    def _csrf(self) -> str:
        if self._client is None:
            return ""
        return (self._client.cookies.get("JSESSIONID") or "").strip('"')

    async def warm_up(self) -> None:
        """Let LinkedIn top up its per-visit cookies, and catch a dead session."""
        if self._client is None:
            await self.start()
        assert self._client is not None
        if self._warmed:
            return

        try:
            response = await self._client.get(self._config.bootstrap_url)
        except httpx.HTTPError:
            logger.warning("could not warm up the Voyager session", exc_info=True)
            return

        if is_session_revoked(response):
            self.last_error = "LinkedIn revoked the session cookie"
            raise UpstreamAuthError(
                "LinkedIn has revoked this session - it answered with "
                "'set-cookie: li_at=delete me'. Mint a new one with "
                "'python scripts/login.py'."
            )
        self._warmed = True

    async def get(
        self, path: str, params: dict[str, Any], *, referer: str | None = None
    ) -> dict[str, Any]:
        """GET one Dash endpoint, mapping transport failures onto AppErrors."""
        if self._client is None:
            await self.start()
        assert self._client is not None

        url = self._config.url(path)
        headers = {"csrf-token": self._csrf}
        if referer:
            headers["referer"] = referer

        try:
            response = await self._client.get(url, params=params, headers=headers)
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout(f"Timed out calling {path}.") from exc
        except httpx.HTTPError as exc:
            raise UpstreamAuthError(f"Voyager request failed: {exc}") from exc

        self._raise_for_status(response, path, params)

        try:
            return response.json()
        except ValueError as exc:
            self._warmed = False
            self.last_error = "Voyager returned a non-JSON body"
            raise UpstreamAuthError(
                "LinkedIn returned HTML instead of JSON, which means the session "
                "was rejected mid-request."
            ) from exc

    def _raise_for_status(
        self, response: httpx.Response, path: str, params: dict[str, Any]
    ) -> None:
        status = response.status_code
        identity = params.get("memberIdentity") or params.get("profileUrn") or "?"

        if status == 404:
            raise ProfileNotFound(f"LinkedIn has no profile for '{identity}'.")

        if status == HTTP_GONE:
            raise UpstreamEndpointGone(
                f"LinkedIn has retired the Voyager endpoint '{path}' (HTTP 410). "
                "Endpoint paths live in config.json under 'voyager' and can be "
                "repointed without a code change."
            )

        if status == 429:
            raise RateLimited("LinkedIn is rate-limiting this account. Back off.")

        if status == HTTP_BLOCKED:
            self._warmed = False
            self.last_error = "LinkedIn returned 999 (request blocked)"
            raise UpstreamAuthError(
                "LinkedIn returned HTTP 999, meaning it classified this request as "
                "automated. A residential proxy and a slower rate are the usual "
                "remedies."
            )

        if status in (401, 403) or response.is_redirect:
            self._warmed = False
            self.last_error = f"Voyager returned HTTP {status}"
            raise UpstreamAuthError(
                f"LinkedIn rejected the request (HTTP {status}). The session is "
                "expired, or this profile is not visible to the account."
            )

        if status >= 400:
            raise UpstreamAuthError(f"Voyager returned an unexpected HTTP {status}.")
