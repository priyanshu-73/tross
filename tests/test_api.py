"""End-to-end HTTP tests against a stubbed provider.

The provider seam means the whole API surface - auth, validation, caching, rate
limiting, error rendering - is testable without a browser or a LinkedIn account.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import ProfileNotFound
from app.models import ExperienceItem, LinkedInProfile, ProviderHealth
from app.providers.base import ProfileProvider

API_KEY = "test-key"
PROFILE_URL = "https://www.linkedin.com/in/ada-lovelace/"


class FakeProvider(ProfileProvider):
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0
        self.raises: Exception | None = None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(name=self.name, configured=True, authenticated=True)

    async def fetch_profile(self, *, public_id, profile_url, input_url) -> LinkedInProfile:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return LinkedInProfile(
            input_url=input_url,
            profile_url=profile_url,
            public_id=public_id,
            full_name="Ada Lovelace",
            headline="Principal Engineer",
            location="Bengaluru, Karnataka, India",
            about="Mathematician and engineer.",
            experience=[ExperienceItem(title="Principal Engineer", company="Acme Corp")],
            source=self.name,
        )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("API_KEYS", API_KEY)
    monkeypatch.setenv("RATE_LIMIT_REQUESTS", "5")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    monkeypatch.setenv("CACHE_TTL_SECONDS", "300")
    monkeypatch.setenv("LINKEDIN_LI_AT", "")
    monkeypatch.setenv("LINKEDIN_EMAIL", "")
    monkeypatch.setenv("LINKEDIN_PASSWORD", "")


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def client(env, provider, monkeypatch):
    from app import main
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(main, "build_provider", lambda settings: provider)

    app = main.create_app()
    # raise_server_exceptions=False so the app's own 500 handler renders the
    # response instead of TestClient re-raising into the test.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client

    get_settings.cache_clear()


def auth() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


# --- auth -------------------------------------------------------------------


def test_health_needs_no_key(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["provider"]["name"] == "fake"


def test_missing_key_is_rejected(client):
    response = client.get("/api/v1/profile", params={"url": PROFILE_URL})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_api_key"


def test_wrong_key_is_rejected(client):
    response = client.get(
        "/api/v1/profile", params={"url": PROFILE_URL}, headers={"X-API-Key": "nope"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "invalid_api_key"


# --- happy path -------------------------------------------------------------


def test_returns_a_profile(client):
    response = client.get("/api/v1/profile", params={"url": PROFILE_URL}, headers=auth())
    assert response.status_code == 200

    body = response.json()
    assert body["success"] is True
    assert body["data"]["full_name"] == "Ada Lovelace"
    assert body["data"]["public_id"] == "ada-lovelace"
    assert body["data"]["experience"][0]["company"] == "Acme Corp"
    assert body["meta"]["provider"] == "fake"
    assert body["meta"]["cached"] is False
    assert response.headers["X-Cache"] == "MISS"
    assert response.headers["X-Request-ID"]


def test_post_accepts_a_json_body(client):
    response = client.post("/api/v1/profile", json={"url": PROFILE_URL}, headers=auth())
    assert response.status_code == 200
    assert response.json()["data"]["full_name"] == "Ada Lovelace"


def test_second_lookup_is_served_from_cache(client, provider):
    client.get("/api/v1/profile", params={"url": PROFILE_URL}, headers=auth())
    response = client.get("/api/v1/profile", params={"url": PROFILE_URL}, headers=auth())

    assert provider.calls == 1
    assert response.headers["X-Cache"] == "HIT"
    assert response.json()["meta"]["cached"] is True


def test_refresh_bypasses_the_cache(client, provider):
    client.get("/api/v1/profile", params={"url": PROFILE_URL}, headers=auth())
    response = client.get(
        "/api/v1/profile", params={"url": PROFILE_URL, "refresh": "true"}, headers=auth()
    )

    assert provider.calls == 2
    assert response.headers["X-Cache"] == "MISS"


def test_differently_shaped_urls_share_one_cache_entry(client, provider):
    client.get("/api/v1/profile", params={"url": PROFILE_URL}, headers=auth())
    response = client.get(
        "/api/v1/profile",
        params={"url": "linkedin.com/in/ada-lovelace?trk=xyz"},
        headers=auth(),
    )

    assert provider.calls == 1
    # The response still echoes what this caller actually sent.
    assert response.json()["data"]["input_url"] == "linkedin.com/in/ada-lovelace?trk=xyz"


# --- errors -----------------------------------------------------------------


def test_invalid_url_is_a_400(client):
    response = client.get(
        "/api/v1/profile", params={"url": "https://example.com/in/ada"}, headers=auth()
    )
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["error"]["code"] == "invalid_profile_url"


def test_company_url_is_a_400(client):
    response = client.get(
        "/api/v1/profile",
        params={"url": "https://www.linkedin.com/company/acme/"},
        headers=auth(),
    )
    assert response.status_code == 400


def test_missing_url_parameter_is_a_422(client):
    response = client.get("/api/v1/profile", headers=auth())
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_provider_errors_keep_their_status(client, provider):
    provider.raises = ProfileNotFound()
    response = client.get("/api/v1/profile", params={"url": PROFILE_URL}, headers=auth())
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "profile_not_found"


def test_unexpected_provider_errors_become_500_without_leaking_detail(client, provider):
    provider.raises = RuntimeError("psycopg2://user:hunter2@db/internal")
    response = client.get("/api/v1/profile", params={"url": PROFILE_URL}, headers=auth())
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "hunter2" not in response.text


def test_rate_limit_returns_429_with_retry_after(client, monkeypatch):
    for index in range(5):
        response = client.get(
            "/api/v1/profile",
            params={"url": f"https://www.linkedin.com/in/person-{index}/"},
            headers=auth(),
        )
        assert response.status_code == 200

    response = client.get(
        "/api/v1/profile",
        params={"url": "https://www.linkedin.com/in/person-overflow/"},
        headers=auth(),
    )
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limited"
    assert int(response.headers["Retry-After"]) > 0


# --- docs -------------------------------------------------------------------


def test_openapi_schema_is_served(client):
    response = client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/api/v1/profile" in schema["paths"]
    assert "APIKeyHeader" in schema.get("components", {}).get("securitySchemes", {})


def test_root_redirects_to_docs(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (307, 302)
    assert response.headers["location"] == "/docs"


# --- open mode --------------------------------------------------------------


@pytest.fixture
def open_client(env, provider, monkeypatch):
    """The shipped default: API_KEYS empty, so no key is required.

    Depends on `env` for the low test rate limit, then blanks the key it set.
    """
    from app import main
    from app.config import get_settings

    monkeypatch.setenv("API_KEYS", "")
    monkeypatch.setenv("LINKEDIN_LI_AT", "")
    get_settings.cache_clear()
    monkeypatch.setattr(main, "build_provider", lambda settings: provider)

    with TestClient(main.create_app(), raise_server_exceptions=False) as client:
        yield client

    get_settings.cache_clear()


def test_empty_api_keys_leaves_the_api_open(open_client):
    response = open_client.get("/api/v1/profile", params={"url": PROFILE_URL})
    assert response.status_code == 200
    assert response.json()["data"]["full_name"] == "Ada Lovelace"


def test_a_stray_key_is_ignored_rather_than_rejected_when_open(open_client):
    """Callers migrating from a keyed deployment must not start failing."""
    response = open_client.get(
        "/api/v1/profile", params={"url": PROFILE_URL}, headers={"X-API-Key": "anything"}
    )
    assert response.status_code == 200


def test_rate_limiting_still_applies_without_a_key(open_client):
    """With no key to identify callers, the limiter falls back to client IP."""
    for index in range(5):
        assert open_client.get(
            "/api/v1/profile",
            params={"url": f"https://www.linkedin.com/in/person-{index}/"},
        ).status_code == 200

    overflow = open_client.get(
        "/api/v1/profile", params={"url": "https://www.linkedin.com/in/person-x/"}
    )
    assert overflow.status_code == 429
