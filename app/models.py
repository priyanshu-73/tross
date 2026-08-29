"""Public response schemas.

Every field on the profile is optional: LinkedIn profiles are sparse by nature
and visibility varies with the viewer's network distance, so a missing value is
normal data rather than an error.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ExperienceItem(BaseModel):
    title: str | None = Field(None, examples=["Senior Software Engineer"])
    company: str | None = Field(None, examples=["Acme Corp"])
    company_url: str | None = None
    employment_type: str | None = Field(None, examples=["Full-time"])
    location: str | None = Field(None, examples=["Bengaluru, Karnataka, India"])
    start_date: str | None = Field(None, examples=["Jan 2020"])
    end_date: str | None = Field(None, examples=["Present"])
    duration: str | None = Field(None, examples=["4 yrs 2 mos"])
    is_current: bool = False
    description: str | None = None


class EducationItem(BaseModel):
    school: str | None = Field(None, examples=["Indian Institute of Technology"])
    school_url: str | None = None
    degree: str | None = Field(None, examples=["Bachelor of Technology"])
    field_of_study: str | None = Field(None, examples=["Computer Science"])
    start_date: str | None = Field(None, examples=["2016"])
    end_date: str | None = Field(None, examples=["2020"])
    description: str | None = None


class SkillItem(BaseModel):
    name: str
    endorsement_count: int | None = None


class CertificationItem(BaseModel):
    name: str | None = None
    issuer: str | None = None
    issue_date: str | None = Field(None, examples=["Mar 2021"])
    expiration_date: str | None = None
    credential_id: str | None = None
    credential_url: str | None = None


class LanguageItem(BaseModel):
    name: str
    proficiency: str | None = Field(None, examples=["Native or bilingual proficiency"])


class ProfileImages(BaseModel):
    profile_picture_url: str | None = None
    background_image_url: str | None = None


class LinkedInProfile(BaseModel):
    # --- identity ---
    input_url: str
    profile_url: str
    public_id: str
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    headline: str | None = None
    location: str | None = None
    about: str | None = None

    # --- top card extras ---
    current_company: str | None = None
    connections: str | None = Field(None, examples=["500+"])
    followers: str | None = Field(None, examples=["12,431"])
    images: ProfileImages = Field(default_factory=ProfileImages)

    # --- sections ---
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    skills: list[SkillItem] = Field(default_factory=list)
    certifications: list[CertificationItem] = Field(default_factory=list)
    languages: list[LanguageItem] = Field(default_factory=list)

    # --- provenance ---
    source: str = "linkedin_scraper"
    scraped_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResponseMeta(BaseModel):
    provider: str
    cached: bool = False
    duration_ms: int
    request_id: str


class ProfileResponse(BaseModel):
    success: Literal[True] = True
    data: LinkedInProfile
    meta: ResponseMeta


class ErrorBody(BaseModel):
    code: str
    message: str
    details: Any | None = None


class ErrorResponse(BaseModel):
    success: Literal[False] = False
    error: ErrorBody
    request_id: str | None = None


class ProviderHealth(BaseModel):
    name: str
    configured: bool
    authenticated: bool | None = None
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    version: str
    environment: str
    provider: ProviderHealth
    cache_entries: int
