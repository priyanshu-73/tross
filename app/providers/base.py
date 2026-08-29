"""The provider contract every data source implements.

The HTTP layer never talks to Playwright or to a vendor SDK directly; it only
knows this interface. Swapping the credentialed scraper for a compliant paid
API (or a mock, in tests) is therefore a one-line change in `registry.py`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import LinkedInProfile, ProviderHealth


class ProfileProvider(ABC):
    """Resolves a LinkedIn public identifier into a `LinkedInProfile`."""

    name: str = "base"

    async def startup(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Acquire long-lived resources (browser, HTTP client, session)."""

    async def shutdown(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Release whatever `startup` acquired."""

    @abstractmethod
    async def health(self) -> ProviderHealth:
        """Report configuration/authentication state without doing real work."""

    @abstractmethod
    async def fetch_profile(
        self, *, public_id: str, profile_url: str, input_url: str
    ) -> LinkedInProfile:
        """Fetch and normalise one profile.

        Raises:
            AppError: subclasses carry the HTTP status the client should see.
        """
