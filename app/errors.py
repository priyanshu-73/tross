"""Typed application errors that map cleanly onto HTTP responses."""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base class for every error the API knows how to render."""

    status_code: int = 500
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(self, message: str | None = None, details: Any = None) -> None:
        self.message = message or self.message
        self.details = details
        super().__init__(self.message)


class InvalidProfileURL(AppError):
    status_code = 400
    code = "invalid_profile_url"
    message = "The supplied value is not a valid LinkedIn member profile URL."


class MissingAPIKey(AppError):
    status_code = 401
    code = "missing_api_key"
    message = "An API key is required. Send it in the 'X-API-Key' header."


class InvalidAPIKey(AppError):
    status_code = 403
    code = "invalid_api_key"
    message = "The supplied API key is not valid."


class ProfileNotFound(AppError):
    status_code = 404
    code = "profile_not_found"
    message = "No LinkedIn profile exists at that URL."


class ProfileNotAccessible(AppError):
    status_code = 403
    code = "profile_not_accessible"
    message = (
        "The profile exists but is not visible to the authenticated account "
        "(out of network, or restricted by the member's privacy settings)."
    )


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"
    message = "Too many requests. Slow down and retry later."


class ProviderNotConfigured(AppError):
    status_code = 503
    code = "provider_not_configured"
    message = "The data provider is not configured on this deployment."


class UpstreamAuthError(AppError):
    """LinkedIn rejected our session: bad cookie, checkpoint, CAPTCHA, or ban."""

    status_code = 502
    code = "upstream_auth_error"
    message = "Could not establish an authenticated LinkedIn session."


class UpstreamTimeout(AppError):
    status_code = 504
    code = "upstream_timeout"
    message = "Timed out while loading the profile from LinkedIn."


class IncompatibleEventLoop(AppError):
    """The running event loop cannot spawn subprocesses, so Playwright cannot start.

    Windows-only, and always a launch-command problem rather than a code problem:
    only ProactorEventLoop implements subprocess transports there, and uvicorn
    swaps in SelectorEventLoop whenever it needs to spawn workers (`--reload`,
    `--workers N`). Without this check the symptom is a bare NotImplementedError
    from deep inside asyncio, which says nothing about the cause or the fix.
    """

    status_code = 500
    code = "incompatible_event_loop"
    message = "The server is running on an event loop that cannot start a browser."


class UpstreamEndpointGone(AppError):
    """The upstream retired the endpoint we call (HTTP 410).

    Distinct from an auth failure on purpose: the session is valid and retrying
    or refreshing the cookie will not help. Private APIs have no deprecation
    policy, so this is the expected way a Voyager endpoint dies.
    """

    status_code = 502
    code = "upstream_endpoint_gone"
    message = "The upstream API endpoint no longer exists."


class ScrapeFailed(AppError):
    status_code = 502
    code = "scrape_failed"
    message = "Failed to extract the profile from the page returned by LinkedIn."
