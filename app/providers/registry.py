"""Selects and owns the single provider instance for the process lifetime."""

from __future__ import annotations

from app.config import Settings
from app.providers.base import ProfileProvider


def build_provider(settings: Settings) -> ProfileProvider:
    """Import lazily so an unused provider never pulls in its dependencies.

    That matters most for the scraper: selecting `voyager` should not import
    Playwright, let alone launch Chromium.
    """
    if settings.provider == "proxycurl":
        from app.providers.proxycurl import ProxycurlProvider

        return ProxycurlProvider(settings)

    if settings.provider == "linkedin_scraper":
        from app.providers.linkedin_scraper.provider import LinkedInScraperProvider

        return LinkedInScraperProvider(settings)

    if settings.provider == "public":
        from app.providers.public.provider import PublicProvider

        return PublicProvider(settings)

    if settings.provider == "embedded":
        from app.providers.embedded.provider import EmbeddedProvider

        return EmbeddedProvider(settings)

    from app.providers.voyager.provider import VoyagerProvider

    return VoyagerProvider(settings)
