"""HTTP surface: profile lookup plus health."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, Field

from app.api.security import require_api_key
from app.config import Settings, get_settings
from app.errors import RateLimited
from app.models import (
    ErrorResponse,
    HealthResponse,
    LinkedInProfile,
    ProfileResponse,
    ResponseMeta,
)
from app.utils.url import normalize_profile_url

logger = logging.getLogger(__name__)

router = APIRouter()

_ERROR_RESPONSES: dict[int | str, dict] = {
    400: {"model": ErrorResponse, "description": "The URL is not a LinkedIn member profile"},
    401: {"model": ErrorResponse, "description": "Missing API key"},
    403: {"model": ErrorResponse, "description": "Invalid API key, or profile not visible"},
    404: {"model": ErrorResponse, "description": "No such profile"},
    429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
    502: {"model": ErrorResponse, "description": "LinkedIn session or extraction failure"},
    503: {"model": ErrorResponse, "description": "Provider not configured"},
    504: {"model": ErrorResponse, "description": "LinkedIn timed out"},
}


class ProfileRequest(BaseModel):
    url: str = Field(
        ...,
        description="A LinkedIn member profile URL, or a bare public identifier.",
        examples=["https://www.linkedin.com/in/williamhgates/"],
    )
    refresh: bool = Field(
        False, description="Bypass the cache and force a fresh fetch from the provider."
    )


async def _enforce_rate_limit(request: Request, api_key: str) -> None:
    limiter = request.app.state.rate_limiter
    identity = api_key if api_key != "anonymous" else (request.client.host if request.client else "unknown")
    allowed, remaining, retry_after = await limiter.check(identity)
    request.state.rate_remaining = remaining
    if not allowed:
        raise RateLimited(
            f"Rate limit exceeded. Retry in {retry_after}s.",
            details={"retry_after_seconds": retry_after},
        )


async def _resolve(
    request: Request, response: Response, raw_url: str, refresh: bool, api_key: str
) -> ProfileResponse:
    await _enforce_rate_limit(request, api_key)

    started = time.perf_counter()
    provider = request.app.state.provider
    cache = request.app.state.cache

    public_id, profile_url = normalize_profile_url(raw_url)
    cache_key = f"{provider.name}:{public_id}"

    cached: LinkedInProfile | None = None if refresh else await cache.get(cache_key)
    if cached is not None:
        profile = cached.model_copy(update={"input_url": raw_url})
    else:
        logger.info("fetching profile public_id=%s provider=%s", public_id, provider.name)
        profile = await provider.fetch_profile(
            public_id=public_id, profile_url=profile_url, input_url=raw_url
        )
        await cache.set(cache_key, profile)

    response.headers["X-RateLimit-Remaining"] = str(getattr(request.state, "rate_remaining", ""))
    response.headers["X-Cache"] = "HIT" if cached is not None else "MISS"

    return ProfileResponse(
        data=profile,
        meta=ResponseMeta(
            provider=provider.name,
            cached=cached is not None,
            duration_ms=int((time.perf_counter() - started) * 1000),
            request_id=getattr(request.state, "request_id", "-"),
        ),
    )


@router.get(
    "/profile",
    response_model=ProfileResponse,
    responses=_ERROR_RESPONSES,
    summary="Fetch a LinkedIn profile by URL",
    description=(
        "Resolves a LinkedIn member profile URL into structured data: name, headline, "
        "location, about, experience, education, skills, certifications, languages and "
        "profile images. Fields absent from the source profile - or hidden from the "
        "authenticated viewer - come back as `null` or an empty list rather than an error."
    ),
)
async def get_profile(
    request: Request,
    response: Response,
    url: str = Query(
        ...,
        description="LinkedIn profile URL (or bare public identifier).",
        examples=["https://www.linkedin.com/in/williamhgates/"],
    ),
    refresh: bool = Query(False, description="Bypass the cache and refetch."),
    api_key: str = Depends(require_api_key),
) -> ProfileResponse:
    return await _resolve(request, response, url, refresh, api_key)


@router.post(
    "/profile",
    response_model=ProfileResponse,
    responses=_ERROR_RESPONSES,
    summary="Fetch a LinkedIn profile by URL (JSON body)",
    description="Identical to `GET /profile`, for clients that prefer a JSON body.",
)
async def post_profile(
    request: Request,
    response: Response,
    payload: ProfileRequest,
    api_key: str = Depends(require_api_key),
) -> ProfileResponse:
    return await _resolve(request, response, payload.url, payload.refresh, api_key)


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Liveness and provider readiness",
    description=(
        "Unauthenticated. Reports whether the configured provider has credentials and "
        "whether a LinkedIn session is currently established. Returns HTTP 200 with "
        "`status: degraded` when the provider is not usable, so platform health checks "
        "do not flap while credentials are being rotated."
    ),
)
async def health(request: Request) -> HealthResponse:
    settings: Settings = get_settings()
    provider_health = await request.app.state.provider.health()
    return HealthResponse(
        status="ok" if provider_health.configured else "degraded",
        version=settings.version,
        environment=settings.environment,
        provider=provider_health,
        cache_entries=len(request.app.state.cache),
    )
