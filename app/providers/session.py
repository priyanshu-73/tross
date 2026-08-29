"""Obtaining and maintaining a LinkedIn session, without a human in the loop.

A pasted ``li_at`` is a poor credential to build on. LinkedIn rotates the cookie
when a session is used from an address it wasn't issued to, which is exactly
what happens when you copy one out of your laptop's browser and use it from a
server — so the cookie dies, you paste a new one, and it dies again. The
lifetime isn't the problem; the manual copy is.

So the session becomes something the service *owns* rather than something it is
handed. Three sources, tried in order:

1. **A cookie this service minted earlier**, persisted to disk. Issued to this
   machine, so it is the one least likely to be rotated out from under us.
2. **``LINKEDIN_LI_AT``**, if supplied. Still supported — it is the only option
   for accounts with 2FA on, where automated login cannot work.
3. **``LINKEDIN_EMAIL`` + ``LINKEDIN_PASSWORD``**, driving LinkedIn's login form
   over plain HTTP. Slower than the others but self-healing: when a session
   dies mid-request, the caller re-runs this and carries on.

Login is the risky operation, not the credential storage — a sign-in from a
datacentre IP is the likeliest thing to trigger a checkpoint. Everything here is
therefore built to log in **as rarely as possible**: persist aggressively, reuse
first, and re-authenticate only when a request actually proves the session dead.

Checkpoints are detected and reported, never worked around.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.config import Settings
from app.errors import ProviderNotConfigured, UpstreamAuthError, UpstreamTimeout
from app.site_config import AuthConfig

logger = logging.getLogger(__name__)


def extract_login_form(html: str, page_url: str) -> tuple[str | None, dict[str, str]]:
    """Find the real sign-in form and harvest every field it carries.

    The sign-in page ships several forms — a decoy posting to
    ``/uas/login-submit`` with a single token, and the live one posting to
    ``/checkpoint/lg/login-submit`` with ~20 hidden fields (two different CSRF
    tokens, a page instance id, a control id, feature flags). LinkedIn validates
    more than one of them, so naming fields individually is guesswork that goes
    stale.

    Instead: pick the form that actually has a password input, then send back
    everything it contains. New hidden fields are carried automatically, and a
    renamed token costs nothing.

    Returns ``(absolute_action_url, fields)``.
    """
    soup = BeautifulSoup(html, "lxml")

    for form in soup.find_all("form"):
        if form.find("input", attrs={"type": "password"}) is None:
            continue

        fields: dict[str, str] = {}
        for node in form.find_all("input"):
            name = node.get("name")
            if not name:
                continue
            fields[name] = node.get("value") or ""

        action = form.get("action")
        return (urljoin(page_url, action) if action else None), fields

    return None, {}


# LinkedIn revokes a session by telling the client to bin the cookie. It does
# this on a 302 back to the same URL, so a client that ignores it loops forever
# re-sending a cookie the server has already killed.
_REVOKED_VALUES = {"delete me", "deleteme", ""}


def is_session_revoked(response: httpx.Response) -> bool:
    """True when LinkedIn's response explicitly deletes the session cookie.

    This is an unambiguous signal and worth acting on: no retry, backoff, or
    header change will help, because the credential itself is gone. Recognising
    it turns an inscrutable redirect loop into "sign in again".
    """
    for header in response.headers.get_list("set-cookie"):
        name, _, rest = header.partition("=")
        if name.strip().lower() != "li_at":
            continue
        value = rest.split(";")[0].strip().strip('"').lower()
        if value in _REVOKED_VALUES:
            return True
    return False


class CookieStore:
    """Persists the whole LinkedIn cookie jar between runs.

    `li_at` alone is not a usable session. LinkedIn also expects the cookies it
    issues alongside it — `JSESSIONID`, `bcookie`, `bscookie`, `lidc` — and a
    request carrying only `li_at` gets bounced into a redirect loop rather than
    a page. So the jar is stored whole and replayed whole.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)

    def load_all(self) -> dict[str, str]:
        """Every stored cookie. Empty dict when there is nothing usable."""
        try:
            if not self._path.exists():
                return {}
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.debug("could not read cookie store at %s", self._path, exc_info=True)
            return {}

        if not isinstance(data, dict):
            return {}
        # Accept both the current shape and the earlier {"li_at": "..."} one.
        jar = data.get("cookies") if isinstance(data.get("cookies"), dict) else data
        return {
            name: value.strip()
            for name, value in jar.items()
            if isinstance(name, str) and isinstance(value, str) and value.strip()
        }

    def load(self) -> str | None:
        """Just the session cookie, for callers that only need to know it exists."""
        return self.load_all().get("li_at")

    def save(self, li_at: str) -> None:
        self.save_all({"li_at": li_at})

    def save_all(self, cookies: dict[str, str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"cookies": cookies}, indent=2), encoding="utf-8")
            logger.info(
                "persisted %d LinkedIn cookies to %s", len(cookies), self._path
            )
        except OSError:
            # Losing the cache costs a login next boot, not correctness.
            logger.warning("could not persist session to %s", self._path, exc_info=True)

    def clear(self) -> None:
        try:
            self._path.unlink(missing_ok=True)
        except OSError:
            logger.debug("could not clear cookie store", exc_info=True)


class LinkedInAuthenticator:
    """Keeps an httpx client carrying a working ``li_at``."""

    def __init__(self, settings: Settings, config: AuthConfig, user_agent: str) -> None:
        self._settings = settings
        self._config = config
        self._user_agent = user_agent
        self._store = CookieStore(settings.cookie_store_path)
        self.last_error: str | None = None
        self.source: str | None = None

    @property
    def can_login(self) -> bool:
        return self._settings.has_login_credentials

    @property
    def configured(self) -> bool:
        return self.can_login or self._settings.has_li_at or bool(self._store.load())

    # --- public API --------------------------------------------------------

    async def apply(self, client: httpx.AsyncClient) -> None:
        """Install the best session we already have. Logs in only if we have none."""
        stored = self._store.load_all()
        if stored.get("li_at"):
            # Replay the whole jar: li_at on its own triggers a redirect loop.
            for name, value in stored.items():
                client.cookies.set(name, value, domain=".linkedin.com")
            self.source = "stored cookie"
            logger.info("using LinkedIn session from stored cookie (%d cookies)", len(stored))
            return

        if self._settings.has_li_at:
            value = self._settings.linkedin_li_at.get_secret_value().strip()
            self._set_cookie(client, value, source="LINKEDIN_LI_AT")
            return

        if self.can_login:
            await self.refresh(client)
            return

        raise ProviderNotConfigured(
            "No LinkedIn session available. Set LINKEDIN_EMAIL + LINKEDIN_PASSWORD "
            "so the service can sign in itself, or supply LINKEDIN_LI_AT."
        )

    async def refresh(self, client: httpx.AsyncClient) -> None:
        """Discard the current session and mint a new one by logging in."""
        if not self.can_login:
            self._store.clear()
            raise UpstreamAuthError(
                "The LinkedIn session expired and there are no credentials to renew "
                "it with. Set LINKEDIN_EMAIL + LINKEDIN_PASSWORD for automatic "
                "re-authentication, or refresh LINKEDIN_LI_AT by hand."
            )

        self._store.clear()
        # Drop the dead session before signing in: a revoked li_at travels with
        # the login request too, and LinkedIn redirect-loops rather than
        # serving the sign-in page to a client carrying one.
        client.cookies.clear()
        self.source = None

        li_at = await self._login(client)
        # Keep everything LinkedIn set during sign-in, not just the session cookie.
        self._store.save_all({c.name: c.value for c in client.cookies.jar if c.value})
        self._set_cookie(client, li_at, source="fresh login")

    # --- internals ---------------------------------------------------------

    def _set_cookie(self, client: httpx.AsyncClient, value: str, *, source: str) -> None:
        client.cookies.set(self._config.session_cookie, value, domain=".linkedin.com")
        self.source = source
        logger.info("using LinkedIn session from %s", source)

    async def _login(self, client: httpx.AsyncClient) -> str:
        config = self._config
        email = self._settings.linkedin_email or ""
        password = self._settings.linkedin_password.get_secret_value()

        logger.info("signing in to LinkedIn as %s", _mask(email))

        # LinkedIn's login form carries a per-visit CSRF token that must be
        # echoed back with the credentials, so the page has to be fetched first.
        try:
            page = await client.get(
                config.login_page_url,
                headers={"user-agent": self._user_agent},
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("Timed out loading the LinkedIn sign-in page.") from exc
        except httpx.HTTPError as exc:
            raise UpstreamAuthError(f"Could not reach the sign-in page: {exc}") from exc

        action_url, form = extract_login_form(page.text, str(page.url))
        if not form:
            self.last_error = "no sign-in form on the login page"
            raise UpstreamAuthError(
                "LinkedIn's sign-in page contained no HTML form, so the credentials "
                "could not be submitted. LinkedIn now renders sign-in entirely in "
                "JavaScript — the password input has no 'name' and there is no "
                "<form> to post — so HTTP login is not possible against it. Run "
                "'python scripts/login.py' once: it signs in via a real browser and "
                "saves the session where this service reads it."
            )

        # Our credentials override whatever placeholder values the form shipped;
        # every other field is echoed back exactly as LinkedIn sent it.
        form[config.session_key_field] = email
        form[config.session_password_field] = password
        for key, value in config.extra_form_fields.items():
            form.setdefault(key, value)

        submit_url = action_url or config.login_submit_url
        logger.debug("submitting sign-in form to %s with %d fields", submit_url, len(form))

        try:
            response = await client.post(
                submit_url,
                data=form,
                headers={
                    "user-agent": self._user_agent,
                    "content-type": "application/x-www-form-urlencoded",
                    "referer": config.login_page_url,
                },
                follow_redirects=True,
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("Timed out submitting the LinkedIn sign-in form.") from exc
        except httpx.HTTPError as exc:
            raise UpstreamAuthError(f"Sign-in request failed: {exc}") from exc

        self._raise_for_login_failure(response)

        li_at = client.cookies.get(config.session_cookie)
        if not li_at:
            self.last_error = "sign-in produced no session cookie"
            raise UpstreamAuthError(
                "Sign-in completed without LinkedIn issuing a session cookie. This "
                "usually means an unrecognised challenge page was returned."
            )

        self.last_error = None
        logger.info("LinkedIn sign-in succeeded")
        return li_at

    def _raise_for_login_failure(self, response: httpx.Response) -> None:
        config = self._config
        visited = " ".join(
            [str(response.url)] + [str(r.url) for r in response.history]
        ).lower()

        if any(marker in visited for marker in config.challenge_url_markers):
            self.last_error = "security checkpoint (2FA / CAPTCHA / device verification)"
            raise UpstreamAuthError(
                "LinkedIn raised a security checkpoint (2FA, CAPTCHA, or device "
                "verification) during sign-in. It cannot be solved automatically, and "
                "this service will not try to. Sign in to the account manually from a "
                "browser on this network, then supply the resulting cookie via "
                "LINKEDIN_LI_AT — 'python scripts/login.py' does both steps."
            )

        body = response.text.lower()
        if any(marker in body for marker in config.bad_credential_markers):
            self.last_error = "LinkedIn rejected the email or password"
            raise UpstreamAuthError(
                "LinkedIn rejected the credentials in LINKEDIN_EMAIL / "
                "LINKEDIN_PASSWORD."
            )

        if response.status_code >= 400:
            self.last_error = f"sign-in returned HTTP {response.status_code}"
            raise UpstreamAuthError(
                f"LinkedIn's sign-in endpoint returned HTTP {response.status_code}."
            )


def _mask(email: str) -> str:
    name, _, domain = email.partition("@")
    if not domain:
        return "***"
    return f"{name[:2]}***@{domain}"
