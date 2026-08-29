"""Non-secret operational configuration, loaded from ``config.json``.

The split is deliberate:

* ``config.json`` (this module) holds everything that describes *how LinkedIn
  works* - URLs, selectors, the strings that signal an auth wall, browser
  tuning. These change when LinkedIn changes, and being able to edit them in one
  JSON file means adapting to a markup change without touching Python.
* ``Settings`` (``app/config.py``) holds everything environment-specific, above
  all the credentials. Those come from env vars and never enter the repository.

Point ``CONFIG_FILE`` at another path to override the whole file (useful for
tests, or for hot-patching selectors on a deployed instance via a mounted file).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class Viewport(BaseModel):
    width: int = 1440
    height: int = 900


class LinkedInConfig(BaseModel):
    base_url: str = "https://www.linkedin.com"
    feed_path: str = "/feed/"
    login_path: str = "/login"
    profile_path_template: str = "/in/{public_id}/"
    details_path_template: str = "/in/{public_id}/details/{section}/"
    detail_sections: list[str] = Field(
        default_factory=lambda: [
            "experience",
            "education",
            "skills",
            "certifications",
            "languages",
        ]
    )

    @property
    def feed_url(self) -> str:
        return self._join(self.feed_path)

    @property
    def login_url(self) -> str:
        return self._join(self.login_path)

    def profile_url(self, public_id: str) -> str:
        return self._join(self.profile_path_template.format(public_id=public_id))

    def details_url(self, public_id: str, section: str) -> str:
        return self._join(
            self.details_path_template.format(public_id=public_id, section=section)
        )

    def _join(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


class BrowserConfig(BaseModel):
    user_agent: str
    locale: str = "en-US"
    timezone_id: str = "UTC"
    viewport: Viewport = Field(default_factory=Viewport)
    launch_args: list[str] = Field(default_factory=list)
    blocked_resource_types: list[str] = Field(default_factory=lambda: ["image", "media", "font"])
    hydration_selector: str = "main h1"
    hydration_timeout_ms: int = 15_000
    scroll_steps: int = 10
    scroll_delta_px: int = 2_000
    scroll_pause_ms: int = 300


class LoginSelectors(BaseModel):
    username: str = "#username"
    password: str = "#password"
    submit: str = 'button[type="submit"]'
    error: str = "#error-for-password, #error-for-username, .alert-content"


class DetectionConfig(BaseModel):
    authwall_url_markers: list[str] = Field(default_factory=list)
    challenge_url_markers: list[str] = Field(default_factory=list)
    not_found_body_markers: list[str] = Field(default_factory=list)


class AuthConfig(BaseModel):
    """LinkedIn's login form, described rather than hard-coded."""

    login_page_url: str = "https://www.linkedin.com/uas/login"
    login_submit_url: str = "https://www.linkedin.com/uas/login-submit"
    csrf_field: str = "loginCsrfParam"
    session_key_field: str = "session_key"
    session_password_field: str = "session_password"
    session_cookie: str = "li_at"
    extra_form_fields: dict[str, str] = Field(default_factory=dict)
    challenge_url_markers: list[str] = Field(default_factory=list)
    bad_credential_markers: list[str] = Field(default_factory=list)


class VocabularyConfig(BaseModel):
    """LinkedIn's own vocabulary, shared by every provider that maps it."""

    month_names: list[str] = Field(
        default_factory=lambda: [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ]
    )
    language_proficiency: dict[str, str] = Field(default_factory=dict)


class EmbeddedConfig(BaseModel):
    """Reads the JSON LinkedIn embeds in the profile page it serves."""

    profile_url_template: str = "https://www.linkedin.com/in/{public_id}/"
    timeout_seconds: float = 30.0
    headers: dict[str, str] = Field(default_factory=dict)
    entity_types: dict[str, str] = Field(default_factory=dict)
    authwall_markers: list[str] = Field(default_factory=list)

    def profile_url(self, public_id: str) -> str:
        return self.profile_url_template.format(public_id=public_id)


class VoyagerConfig(BaseModel):
    """LinkedIn's Dash REST API - the endpoints linkedin.com's own client calls.

    Paths are configuration rather than constants because LinkedIn retires them
    without notice: the previous generation (`identity/profiles/{id}/profileView`)
    now answers 410. When that happens again, this is a JSON edit.
    """

    base_url: str = "https://www.linkedin.com/voyager/api"
    bootstrap_url: str = "https://www.linkedin.com/feed/"
    profile_path: str = "/identity/dash/profiles"
    profile_decoration_id: str = ""
    skills_path: str = "/identity/dash/profileSkills"
    certifications_path: str = "/identity/dash/profileCertifications"
    languages_path: str = "/identity/dash/profileLanguages"
    page_size: int = 100
    timeout_seconds: float = 30.0
    headers: dict[str, str] = Field(default_factory=dict)
    entity_types: dict[str, str] = Field(default_factory=dict)

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"


class ProxycurlConfig(BaseModel):
    base_url: str = "https://nubela.co/proxycurl/api/v2/linkedin"
    timeout_seconds: float = 60.0


class SiteConfig(BaseModel):
    linkedin: LinkedInConfig = Field(default_factory=LinkedInConfig)
    browser: BrowserConfig
    login_selectors: LoginSelectors = Field(default_factory=LoginSelectors)
    detection: DetectionConfig = Field(default_factory=DetectionConfig)
    proxycurl: ProxycurlConfig = Field(default_factory=ProxycurlConfig)
    voyager: VoyagerConfig = Field(default_factory=VoyagerConfig)
    embedded: EmbeddedConfig = Field(default_factory=EmbeddedConfig)
    vocabulary: VocabularyConfig = Field(default_factory=VocabularyConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)


@lru_cache
def get_site_config() -> SiteConfig:
    path = Path(os.getenv("CONFIG_FILE") or DEFAULT_CONFIG_PATH)
    with path.open(encoding="utf-8") as handle:
        return SiteConfig.model_validate(json.load(handle))
