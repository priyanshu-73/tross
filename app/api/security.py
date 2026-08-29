"""API-key authentication."""

from __future__ import annotations

import hmac

from fastapi import Request, Security
from fastapi.security import APIKeyHeader

from app.config import Settings, get_settings
from app.errors import InvalidAPIKey, MissingAPIKey

API_KEY_HEADER = "X-API-Key"

# Taking this as a dependency is what registers the scheme in the OpenAPI
# document, which in turn gives Swagger UI its "Authorize" button.
# `auto_error=False` because we raise our own typed errors rather than FastAPI's.
api_key_scheme = APIKeyHeader(name=API_KEY_HEADER, auto_error=False)


def _matches(candidate: str, allowed: frozenset[str]) -> bool:
    # compare_digest against every key so the timing does not leak which one hit.
    return any(hmac.compare_digest(candidate, key) for key in allowed)


async def require_api_key(
    request: Request,
    supplied: str | None = Security(api_key_scheme),
) -> str:
    settings: Settings = get_settings()
    if not settings.auth_enabled:
        return "anonymous"

    if not supplied:
        raise MissingAPIKey()
    if not _matches(supplied, settings.api_key_set):
        raise InvalidAPIKey()
    return supplied
