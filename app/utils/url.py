"""Parsing and normalisation of LinkedIn member profile URLs."""

from __future__ import annotations

import re
from urllib.parse import unquote, urlparse

from app.errors import InvalidProfileURL
from app.site_config import get_site_config

# LinkedIn member slugs are unicode-capable (e.g. /in/张伟-1a2b3c4d) so we allow
# anything that is not a delimiter, then bound the length defensively.
_SLUG_RE = re.compile(r"^[^/?#\s]{2,128}$")

_MEMBER_PATH_PREFIXES = ("in", "pub")
_NON_MEMBER_PATHS = {
    "company",
    "school",
    "showcase",
    "groups",
    "jobs",
    "posts",
    "feed",
    "learning",
    "events",
    "newsletters",
    "pulse",
}


def _registrable_host() -> str:
    """The bare host (e.g. 'linkedin.com') derived from config.json."""
    host = urlparse(get_site_config().linkedin.base_url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def normalize_profile_url(raw: str) -> tuple[str, str]:
    """Return ``(public_id, canonical_url)`` for a LinkedIn profile URL.

    Accepts the many shapes users actually paste: locale subdomains
    (``in.linkedin.com``), missing scheme, tracking query strings, trailing
    sub-paths (``/details/experience/``), the legacy ``/pub/`` form, and a bare
    public identifier.

    Raises:
        InvalidProfileURL: if the value is not a member profile.
    """
    if not raw or not raw.strip():
        raise InvalidProfileURL("A profile URL is required.")

    candidate = raw.strip()
    site_host = _registrable_host()

    # A bare slug ("john-doe-12345") with no host is treated as a public id.
    if site_host not in candidate.lower() and "/" not in candidate:
        return _validate_slug(candidate, raw)

    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        candidate = "https://" + candidate

    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower().split(":")[0]
    if not (host == site_host or host.endswith("." + site_host)):
        raise InvalidProfileURL(
            f"Expected a {site_host} URL, got host '{host or 'unknown'}'."
        )

    segments = [s for s in parsed.path.split("/") if s]
    if not segments:
        raise InvalidProfileURL("URL does not point at a member profile (missing /in/).")

    head = segments[0].lower()
    if head in _NON_MEMBER_PATHS:
        raise InvalidProfileURL(
            f"'/{head}/' URLs are not member profiles. This API only resolves "
            f"personal profiles ({site_host}/in/...)."
        )
    if head not in _MEMBER_PATH_PREFIXES or len(segments) < 2:
        raise InvalidProfileURL(
            "URL does not point at a member profile. Expected the form "
            f"{get_site_config().linkedin.base_url}/in/<public-id>."
        )

    return _validate_slug(segments[1], raw)


def _validate_slug(slug: str, raw: str) -> tuple[str, str]:
    public_id = unquote(slug).strip().strip("/")
    if not _SLUG_RE.match(public_id):
        raise InvalidProfileURL(f"'{raw}' does not contain a usable profile identifier.")
    return public_id, get_site_config().linkedin.profile_url(public_id)
