"""The Voyager provider — LinkedIn's own API, no browser, all ten fields.

Four GETs per profile:

* ``identity/dash/profiles``            — profile, positions, educations,
                                          companies, schools, geo (one call,
                                          via the FullProfileWithEntities
                                          decoration)
* ``identity/dash/profileSkills``       — the complete skill list
* ``identity/dash/profileCertifications``
* ``identity/dash/profileLanguages``

The last three are best-effort: a member with no certifications is not a failed
request, and losing an optional section is not worth failing the whole profile
over. The first is required — without it there is no profile.

Everything arrives normalised (a flat ``included[]`` of typed entities), so the
mapper filters by ``$type`` rather than walking a nested tree LinkedIn is free
to restructure.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.config import Settings
from app.errors import AppError, ProviderNotConfigured, ScrapeFailed, UpstreamAuthError
from app.models import LinkedInProfile, ProviderHealth
from app.providers.base import ProfileProvider
from app.providers.session import LinkedInAuthenticator
from app.providers.voyager import mapper
from app.providers.voyager.client import VoyagerClient
from app.site_config import get_site_config

logger = logging.getLogger(__name__)


class VoyagerProvider(ProfileProvider):
    name = "voyager"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        site = get_site_config()
        self._config = site.voyager
        self._vocab = site.vocabulary
        self._auth = LinkedInAuthenticator(settings, site.auth, site.browser.user_agent)
        self._client = VoyagerClient(
            settings, self._config, self._auth, site.browser.user_agent
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_scrapes)
        self._lock = asyncio.Lock()
        self._throttle_lock = asyncio.Lock()
        self._last_hit = 0.0
        self._startup_error: str | None = None

    # --- lifecycle ---------------------------------------------------------

    async def startup(self) -> None:
        if not self._auth.configured:
            logger.warning("no LinkedIn session configured - /profile will return 503")
            return
        try:
            await self._client.start()
        except AppError as exc:
            self._startup_error = exc.message
            logger.error("voyager startup failed: %s", exc.message)

    async def shutdown(self) -> None:
        await self._client.close()

    async def health(self) -> ProviderHealth:
        configured = self._auth.configured and not self._startup_error
        return ProviderHealth(
            name=self.name,
            configured=configured,
            authenticated=self._client.authenticated if configured else None,
            detail=self._startup_error
            or self._client.last_error
            or self._auth.last_error
            or (
                f"session source: {self._auth.source}"
                if configured
                else "run 'python scripts/login.py', or set LINKEDIN_LI_AT"
            ),
        )

    # --- main entry point --------------------------------------------------

    async def fetch_profile(
        self, *, public_id: str, profile_url: str, input_url: str
    ) -> LinkedInProfile:
        if not self._auth.configured:
            raise ProviderNotConfigured(
                "No LinkedIn session. Run 'python scripts/login.py' to mint one, "
                "or set LINKEDIN_LI_AT."
            )

        async with self._semaphore:
            await self._throttle()
            try:
                return await self._fetch(public_id, profile_url, input_url)
            except UpstreamAuthError:
                # A session can die between requests. Renew once, then give up.
                if not self._auth.can_login and self._auth.source is None:
                    raise
                logger.info("voyager session rejected; re-authenticating once")
                async with self._lock:
                    await self._client.reauthenticate()
                self._startup_error = None
                return await self._fetch(public_id, profile_url, input_url)

    # --- internals ---------------------------------------------------------

    async def _throttle(self) -> None:
        interval = self._settings.scrape_min_interval_seconds
        if interval <= 0:
            return
        async with self._throttle_lock:
            wait = interval - (time.monotonic() - self._last_hit)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hit = time.monotonic()

    async def _fetch(
        self, public_id: str, profile_url: str, input_url: str
    ) -> LinkedInProfile:
        await self._client.warm_up()

        params = {"q": "memberIdentity", "memberIdentity": public_id}
        if self._config.profile_decoration_id:
            params["decorationId"] = self._config.profile_decoration_id

        payload = await self._client.get(
            self._config.profile_path, params, referer=profile_url
        )
        entities = mapper.included(payload)
        if not entities:
            raise ScrapeFailed(
                "LinkedIn returned an empty Dash response for this profile. It may "
                "be restricted, or the decoration id in config.json may be stale."
            )

        profile_urn = self._profile_urn(entities, public_id)
        if profile_urn:
            entities.extend(await self._sections(profile_urn, profile_url))
        # Section responses repeat entities the profile call already returned.
        entities = mapper.dedupe(entities)

        try:
            return mapper.map_profile(
                entities,
                public_id=public_id,
                profile_url=profile_url,
                input_url=input_url,
                config=self._config,
                vocab=self._vocab,
                source=self.name,
            )
        except ValueError as exc:
            raise ScrapeFailed(
                f"The Dash payload could not be mapped ({exc}). LinkedIn has most "
                "likely changed the entity shape; type fragments are in config.json."
            ) from exc

    def _profile_urn(self, entities: list[dict], public_id: str) -> str | None:
        for entity in mapper.of_type(entities, self._config.entity_types.get("profile", "")):
            if entity.get("publicIdentifier") == public_id or len(entities) == 1:
                urn = entity.get("entityUrn")
                return urn if isinstance(urn, str) else None
        first = mapper.of_type(entities, self._config.entity_types.get("profile", ""))
        urn = first[0].get("entityUrn") if first else None
        return urn if isinstance(urn, str) else None

    async def _sections(self, profile_urn: str, referer: str) -> list[dict]:
        """Skills, certifications and languages — each its own collection."""
        extra: list[dict] = []
        for path in (
            self._config.skills_path,
            self._config.certifications_path,
            self._config.languages_path,
        ):
            params: dict[str, object] = {"q": "viewee", "profileUrn": profile_urn}
            if path == self._config.skills_path:
                params["count"] = self._config.page_size
            try:
                payload = await self._client.get(path, params, referer=referer)
            except UpstreamAuthError:
                raise  # a dead session is real; the caller must see it
            except AppError as exc:
                logger.info("optional section %s unavailable: %s", path, exc.message)
                continue
            except Exception:
                logger.warning("optional section %s failed", path, exc_info=True)
                continue
            extra.extend(mapper.included(payload))
        return extra
