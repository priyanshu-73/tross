"""Proxycurl provider - the compliant, paid alternative to scraping.

Exists to prove the point of `ProfileProvider`: the routes, models, cache and
error contract are identical whichever provider is selected, so a deployment can
move off credentialed scraping by changing `PROVIDER=proxycurl` and adding an
API key. Enrichment vendors like this one hold their own data licences, so no
LinkedIn account is involved and nothing is rate-limited by network distance.
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
    ScrapeFailed,
    UpstreamAuthError,
    UpstreamTimeout,
)
from app.models import (
    CertificationItem,
    EducationItem,
    ExperienceItem,
    LanguageItem,
    LinkedInProfile,
    ProfileImages,
    ProviderHealth,
    SkillItem,
)
from app.providers.base import ProfileProvider
from app.site_config import get_site_config

logger = logging.getLogger(__name__)

_MONTHS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def _fmt_date(node: dict[str, Any] | None) -> str | None:
    """Proxycurl dates are {'day': 1, 'month': 3, 'year': 2021}."""
    if not node or not node.get("year"):
        return None
    month = _MONTHS.get(node.get("month") or 0)
    return f"{month} {node['year']}" if month else str(node["year"])


class ProxycurlProvider(ProfileProvider):
    name = "proxycurl"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._config = get_site_config().proxycurl
        self._client: httpx.AsyncClient | None = None

    async def startup(self) -> None:
        if self._settings.proxycurl_api_key:
            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def health(self) -> ProviderHealth:
        configured = bool(self._settings.proxycurl_api_key)
        return ProviderHealth(
            name=self.name,
            configured=configured,
            authenticated=configured or None,
            detail=None if configured else "PROXYCURL_API_KEY not set",
        )

    async def fetch_profile(
        self, *, public_id: str, profile_url: str, input_url: str
    ) -> LinkedInProfile:
        if self._client is None or not self._settings.proxycurl_api_key:
            raise ProviderNotConfigured("PROXYCURL_API_KEY is not set on this deployment.")

        try:
            response = await self._client.get(
                self._config.base_url,
                params={
                    "linkedin_profile_url": profile_url,
                    "skills": "include",
                    "extra": "include",
                },
                headers={
                    "Authorization": f"Bearer {self._settings.proxycurl_api_key.get_secret_value()}"
                },
            )
        except httpx.TimeoutException as exc:
            raise UpstreamTimeout("Proxycurl did not respond in time.") from exc
        except httpx.HTTPError as exc:
            raise ScrapeFailed(f"Proxycurl request failed: {exc}") from exc

        if response.status_code == 404:
            raise ProfileNotFound(f"Proxycurl has no profile for '/in/{public_id}'.")
        if response.status_code in (401, 403):
            raise UpstreamAuthError("Proxycurl rejected the API key.")
        if response.status_code == 429:
            raise RateLimited("Proxycurl rate limit or credit balance exhausted.")
        if response.status_code >= 400:
            raise ScrapeFailed(f"Proxycurl returned HTTP {response.status_code}.")

        return self._to_profile(response.json(), public_id, profile_url, input_url)

    def _to_profile(
        self, payload: dict[str, Any], public_id: str, profile_url: str, input_url: str
    ) -> LinkedInProfile:
        experiences = [
            ExperienceItem(
                title=item.get("title"),
                company=item.get("company"),
                company_url=item.get("company_linkedin_profile_url"),
                location=item.get("location"),
                start_date=_fmt_date(item.get("starts_at")),
                end_date=_fmt_date(item.get("ends_at")) or "Present",
                is_current=item.get("ends_at") is None,
                description=item.get("description"),
            )
            for item in payload.get("experiences") or []
        ]

        education = [
            EducationItem(
                school=item.get("school"),
                school_url=item.get("school_linkedin_profile_url"),
                degree=item.get("degree_name"),
                field_of_study=item.get("field_of_study"),
                start_date=_fmt_date(item.get("starts_at")),
                end_date=_fmt_date(item.get("ends_at")),
                description=item.get("description"),
            )
            for item in payload.get("education") or []
        ]

        certifications = [
            CertificationItem(
                name=item.get("name"),
                issuer=item.get("authority"),
                issue_date=_fmt_date(item.get("starts_at")),
                expiration_date=_fmt_date(item.get("ends_at")),
                credential_id=item.get("license_number"),
                credential_url=item.get("url"),
            )
            for item in payload.get("certifications") or []
        ]

        location = ", ".join(
            part
            for part in (payload.get("city"), payload.get("state"), payload.get("country_full_name"))
            if part
        ) or None

        current = next((e for e in experiences if e.is_current), None)

        return LinkedInProfile(
            input_url=input_url,
            profile_url=profile_url,
            public_id=public_id,
            full_name=payload.get("full_name"),
            first_name=payload.get("first_name"),
            last_name=payload.get("last_name"),
            headline=payload.get("headline") or payload.get("occupation"),
            location=location,
            about=payload.get("summary"),
            current_company=current.company if current else None,
            connections=str(payload["connections"]) if payload.get("connections") else None,
            followers=str(payload["follower_count"]) if payload.get("follower_count") else None,
            images=ProfileImages(
                profile_picture_url=payload.get("profile_pic_url"),
                background_image_url=payload.get("background_cover_image_url"),
            ),
            experience=experiences,
            education=education,
            skills=[SkillItem(name=s) for s in payload.get("skills") or [] if s],
            certifications=certifications,
            languages=[LanguageItem(name=lang) for lang in payload.get("languages") or [] if lang],
            source=self.name,
        )
