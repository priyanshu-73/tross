"""Session acquisition: precedence, automatic login, and checkpoint handling.

All traffic is served by `httpx.MockTransport`, so nothing here touches a real
LinkedIn account.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.config import Settings
from app.errors import ProviderNotConfigured, UpstreamAuthError
from app.providers.session import CookieStore, LinkedInAuthenticator
from app.site_config import get_site_config

# Mirrors the real sign-in page: a decoy form carrying a single token, then the
# live one with a password input and a pile of hidden fields.
LOGIN_PAGE = """<html><body>
  <form action="/uas/login-submit">
    <input type="hidden" name="loginCsrfParam" value="decoy-token">
    <input type="hidden" name="trk" value="guest">
  </form>
  <form action="/checkpoint/lg/login-submit">
    <input type="hidden" name="csrfToken" value="ajax:999">
    <input type="hidden" name="loginCsrfParam" value="real-token-abc">
    <input type="hidden" name="pageInstance" value="urn:li:page:xyz">
    <input type="hidden" name="controlId" value="ctrl-1">
    <input type="text" name="session_key" value="">
    <input type="password" name="session_password" value="">
  </form>
</body></html>"""


def make_settings(tmp_path, **overrides) -> Settings:
    base = {
        "_env_file": None,
        "provider": "embedded",
        "linkedin_li_at": None,
        "linkedin_email": None,
        "linkedin_password": None,
        "cookie_store_path": str(tmp_path / "cookies.json"),
    }
    base.update(overrides)
    return Settings(**base)


def make_auth(settings) -> LinkedInAuthenticator:
    site = get_site_config()
    return LinkedInAuthenticator(settings, site.auth, site.browser.user_agent)


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def login_handler(
    *, csrf_page: str = LOGIN_PAGE, set_cookie: bool = True, redirect_to: str | None = None
):
    """A stand-in LinkedIn that accepts the login form."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=csrf_page)
        headers = {}
        if set_cookie:
            headers["set-cookie"] = "li_at=minted-cookie-123; Domain=.linkedin.com; Path=/"
        if redirect_to:
            return httpx.Response(
                302, headers={**headers, "location": redirect_to}, text=""
            )
        return httpx.Response(200, headers=headers, text="<html>feed</html>")

    return handler


# --- cookie store -----------------------------------------------------------


def test_the_real_form_is_picked_over_the_decoy():
    """The page ships a decoy form; only one has a password input."""
    from app.providers.session import extract_login_form

    action, fields = extract_login_form(LOGIN_PAGE, "https://www.linkedin.com/uas/login")
    assert action == "https://www.linkedin.com/checkpoint/lg/login-submit"
    assert fields["loginCsrfParam"] == "real-token-abc"
    assert "decoy-token" not in fields.values()


def test_every_hidden_field_is_carried_back():
    """LinkedIn validates several tokens, so the whole form is echoed."""
    from app.providers.session import extract_login_form

    _, fields = extract_login_form(LOGIN_PAGE, "https://www.linkedin.com/uas/login")
    assert {"csrfToken", "loginCsrfParam", "pageInstance", "controlId"} <= set(fields)


def test_a_page_with_no_password_form_yields_nothing():
    from app.providers.session import extract_login_form

    action, fields = extract_login_form("<html><form></form></html>", "https://x/")
    assert action is None and fields == {}


def test_cookie_store_round_trips(tmp_path):
    store = CookieStore(tmp_path / "nested" / "cookies.json")
    assert store.load() is None
    store.save("abc123")
    assert store.load() == "abc123"
    store.clear()
    assert store.load() is None


def test_cookie_store_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text("{not json", encoding="utf-8")
    assert CookieStore(path).load() is None


# --- configuration ----------------------------------------------------------


def test_no_credentials_at_all_is_not_configured(tmp_path):
    assert make_auth(make_settings(tmp_path)).configured is False


def test_email_and_password_alone_is_configured(tmp_path):
    settings = make_settings(tmp_path, linkedin_email="a@b.com", linkedin_password="pw")
    auth = make_auth(settings)
    assert auth.configured is True
    assert auth.can_login is True


def test_cookie_alone_is_configured_but_cannot_self_renew(tmp_path):
    auth = make_auth(make_settings(tmp_path, linkedin_li_at="cookie"))
    assert auth.configured is True
    assert auth.can_login is False


@pytest.mark.asyncio
async def test_applying_with_no_credentials_raises(tmp_path):
    auth = make_auth(make_settings(tmp_path))
    async with client_for(login_handler()) as client:
        with pytest.raises(ProviderNotConfigured):
            await auth.apply(client)


# --- source precedence ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_previously_minted_cookie_wins_over_the_env_var(tmp_path):
    """The stored cookie was issued to *this* machine, so it is the better bet."""
    store_path = tmp_path / "cookies.json"
    store_path.write_text(json.dumps({"li_at": "stored-cookie"}), encoding="utf-8")

    settings = make_settings(
        tmp_path, linkedin_li_at="env-cookie", cookie_store_path=str(store_path)
    )
    auth = make_auth(settings)

    async with client_for(login_handler()) as client:
        await auth.apply(client)
        assert client.cookies.get("li_at") == "stored-cookie"
    assert auth.source == "stored cookie"


@pytest.mark.asyncio
async def test_env_cookie_is_used_when_nothing_is_stored(tmp_path):
    auth = make_auth(make_settings(tmp_path, linkedin_li_at="env-cookie"))
    async with client_for(login_handler()) as client:
        await auth.apply(client)
        assert client.cookies.get("li_at") == "env-cookie"
    assert auth.source == "LINKEDIN_LI_AT"


@pytest.mark.asyncio
async def test_login_happens_only_when_there_is_nothing_to_reuse(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return login_handler()(request)

    settings = make_settings(tmp_path, linkedin_email="a@b.com", linkedin_password="pw")
    auth = make_auth(settings)

    async with client_for(handler) as client:
        await auth.apply(client)
        assert client.cookies.get("li_at") == "minted-cookie-123"

    assert calls == ["GET", "POST"], "login should be one page fetch plus one submit"
    assert auth.source == "fresh login"


@pytest.mark.asyncio
async def test_a_minted_cookie_is_persisted_for_next_time(tmp_path):
    settings = make_settings(tmp_path, linkedin_email="a@b.com", linkedin_password="pw")
    auth = make_auth(settings)

    async with client_for(login_handler()) as client:
        await auth.refresh(client)

    assert CookieStore(settings.cookie_store_path).load() == "minted-cookie-123"


# --- failure modes ----------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_without_credentials_explains_the_dead_end(tmp_path):
    auth = make_auth(make_settings(tmp_path, linkedin_li_at="stale"))
    async with client_for(login_handler()) as client:
        with pytest.raises(UpstreamAuthError) as excinfo:
            await auth.refresh(client)
    assert "LINKEDIN_EMAIL" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_login_endpoint_is_not_mistaken_for_a_checkpoint(tmp_path):
    """LinkedIn signs you in *at* /checkpoint/lg/login-submit - that is success."""
    settings = make_settings(tmp_path, linkedin_email="a@b.com", linkedin_password="pw")
    auth = make_auth(settings)

    async with client_for(login_handler()) as client:
        await auth.refresh(client)
    assert auth.source == "fresh login"


@pytest.mark.asyncio
async def test_a_checkpoint_is_reported_not_worked_around(tmp_path):
    settings = make_settings(tmp_path, linkedin_email="a@b.com", linkedin_password="pw")
    auth = make_auth(settings)

    handler = login_handler(
        set_cookie=False,
        redirect_to="https://www.linkedin.com/checkpoint/challenge/AgH123",
    )
    async with client_for(handler) as client:
        with pytest.raises(UpstreamAuthError) as excinfo:
            await auth.refresh(client)

    message = str(excinfo.value)
    assert "checkpoint" in message.lower()
    assert "scripts/login.py" in message
    assert auth.last_error and "checkpoint" in auth.last_error


@pytest.mark.asyncio
async def test_rejected_credentials_are_reported_plainly(tmp_path):
    settings = make_settings(tmp_path, linkedin_email="a@b.com", linkedin_password="wrong")
    auth = make_auth(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, text=LOGIN_PAGE)
        return httpx.Response(200, text="<p>Wrong email or password. Try again.</p>")

    async with client_for(handler) as client:
        with pytest.raises(UpstreamAuthError) as excinfo:
            await auth.refresh(client)
    assert "rejected the credentials" in str(excinfo.value)


@pytest.mark.asyncio
async def test_a_changed_login_form_says_so(tmp_path):
    settings = make_settings(tmp_path, linkedin_email="a@b.com", linkedin_password="pw")
    auth = make_auth(settings)

    async with client_for(login_handler(csrf_page="<html>no form here</html>")) as client:
        with pytest.raises(UpstreamAuthError) as excinfo:
            await auth.refresh(client)

    message = str(excinfo.value)
    assert "no HTML form" in message
    assert "scripts/login.py" in message


@pytest.mark.asyncio
async def test_a_login_that_issues_no_cookie_is_a_failure(tmp_path):
    settings = make_settings(tmp_path, linkedin_email="a@b.com", linkedin_password="pw")
    auth = make_auth(settings)

    async with client_for(login_handler(set_cookie=False)) as client:
        with pytest.raises(UpstreamAuthError) as excinfo:
            await auth.refresh(client)
    assert "without LinkedIn issuing a session cookie" in str(excinfo.value)


@pytest.mark.asyncio
async def test_refresh_discards_the_stale_cookie_before_retrying(tmp_path):
    """A dead cookie must not survive a failed refresh and get reused."""
    store_path = tmp_path / "cookies.json"
    store_path.write_text(json.dumps({"li_at": "dead"}), encoding="utf-8")
    settings = make_settings(tmp_path, cookie_store_path=str(store_path))
    auth = make_auth(settings)

    async with client_for(login_handler()) as client:
        with pytest.raises(UpstreamAuthError):
            await auth.refresh(client)

    assert CookieStore(store_path).load() is None


# --- session revocation -----------------------------------------------------


def _response_with(*set_cookie: str) -> httpx.Response:
    headers = [("set-cookie", value) for value in set_cookie]
    return httpx.Response(302, headers=headers)


def test_explicit_cookie_deletion_is_recognised_as_revocation():
    """LinkedIn kills a session with `set-cookie: li_at=delete me`."""
    from app.providers.session import is_session_revoked

    assert is_session_revoked(_response_with("li_at=delete me; Path=/")) is True
    assert is_session_revoked(_response_with('li_at="delete me"; Path=/')) is True
    assert is_session_revoked(_response_with("li_at=; Path=/; Max-Age=0")) is True


def test_a_real_cookie_value_is_not_revocation():
    from app.providers.session import is_session_revoked

    assert is_session_revoked(_response_with("li_at=AQEDAT123real; Path=/")) is False
    assert is_session_revoked(_response_with("lidc=delete me")) is False
    assert is_session_revoked(httpx.Response(200)) is False


def test_cookie_store_reads_both_the_old_and_new_file_shapes(tmp_path):
    """Older runs wrote {"li_at": ...}; the store now keeps the whole jar."""
    legacy = tmp_path / "legacy.json"
    legacy.write_text(json.dumps({"li_at": "abc"}), encoding="utf-8")
    assert CookieStore(legacy).load() == "abc"

    modern = tmp_path / "modern.json"
    CookieStore(modern).save_all({"li_at": "abc", "JSESSIONID": "ajax:1"})
    assert CookieStore(modern).load() == "abc"
    assert CookieStore(modern).load_all() == {"li_at": "abc", "JSESSIONID": "ajax:1"}


@pytest.mark.asyncio
async def test_the_whole_jar_is_replayed_not_just_the_session_cookie(tmp_path):
    """li_at alone gets redirect-looped; LinkedIn wants its other cookies too."""
    store_path = tmp_path / "cookies.json"
    settings = make_settings(tmp_path, cookie_store_path=str(store_path))
    CookieStore(store_path).save_all(
        {"li_at": "abc", "JSESSIONID": "ajax:1", "bcookie": "v=2"}
    )

    auth = make_auth(settings)
    async with client_for(login_handler()) as client:
        await auth.apply(client)
        names = {c.name for c in client.cookies.jar}

    assert {"li_at", "JSESSIONID", "bcookie"} <= names


@pytest.mark.asyncio
async def test_refresh_clears_a_revoked_cookie_before_signing_in(tmp_path):
    """A dead li_at travels with the login request and breaks it too."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("cookie", ""))
        return login_handler()(request)

    settings = make_settings(tmp_path, linkedin_email="a@b.com", linkedin_password="pw")
    auth = make_auth(settings)

    async with client_for(handler) as client:
        client.cookies.set("li_at", "revoked-value", domain=".linkedin.com")
        await auth.refresh(client)

    assert not any("revoked-value" in cookie for cookie in seen), (
        "the dead cookie must not be sent with the sign-in request"
    )
