"""Application factory, middleware, and error rendering."""

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from app.api.routes import router
from app.config import Settings, get_settings
from app.errors import AppError
from app.models import ErrorBody, ErrorResponse
from app.providers.registry import build_provider
from app.services.cache import TTLCache
from app.services.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"

DESCRIPTION = """
Structured LinkedIn profile data from a profile URL.

**Authentication** - send your key in the `X-API-Key` header. Use the *Authorize*
button above to set it for the interactive calls on this page.

**Quick start**

```bash
curl -H "X-API-Key: $API_KEY" \\
  "$BASE_URL/api/v1/profile?url=https://www.linkedin.com/in/williamhgates/"
```

**Notes**

* Every profile field is optional. LinkedIn shows different data to different
  viewers, so absent values are returned as `null` / `[]`, never as an error.
* Successful lookups are cached (24h by default). Pass `refresh=true` to bypass.
* This service reads LinkedIn through an authenticated session; see the README
  for the rate limits, legal position, and failure modes that implies.
"""


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    _configure_logging(settings)

    if not settings.auth_enabled:
        logger.warning(
            "API_KEYS is empty - the API is UNAUTHENTICATED. Set API_KEYS before "
            "exposing this deployment publicly."
        )

    app.state.cache = TTLCache(
        ttl_seconds=settings.cache_ttl_seconds, max_entries=settings.cache_max_entries
    )
    app.state.rate_limiter = RateLimiter(
        limit=settings.rate_limit_requests, window_seconds=settings.rate_limit_window_seconds
    )
    app.state.provider = build_provider(settings)

    try:
        await app.state.provider.startup()
    except Exception:
        # A provider that cannot start must not stop the service from booting -
        # /health needs to be reachable to explain what is wrong.
        logger.exception("provider startup failed; /health will report degraded")

    logger.info("started with provider=%s environment=%s", settings.provider, settings.environment)
    try:
        yield
    finally:
        await app.state.provider.shutdown()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        log = logger.warning if exc.status_code < 500 else logger.error
        log("request_id=%s %s: %s", request_id, exc.code, exc.message)

        headers = {}
        if exc.status_code == 429 and isinstance(exc.details, dict):
            headers["Retry-After"] = str(exc.details.get("retry_after_seconds", 60))

        body = ErrorResponse(
            error=ErrorBody(code=exc.code, message=exc.message, details=exc.details),
            request_id=request_id,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(mode="json"),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        body = ErrorResponse(
            error=ErrorBody(
                code="validation_error",
                message="The request parameters were not valid.",
                details=exc.errors(),
            ),
            request_id=getattr(request.state, "request_id", None),
        )
        return JSONResponse(status_code=422, content=body.model_dump(mode="json"))

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.exception("request_id=%s unhandled error", request_id)
        body = ErrorResponse(
            error=ErrorBody(
                code="internal_error",
                message="An unexpected error occurred.",
            ),
            request_id=request_id,
        )
        return JSONResponse(status_code=500, content=body.model_dump(mode="json"))

    app.include_router(router, prefix=API_PREFIX, tags=["profiles"])

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/docs")

    return app


app = create_app()
