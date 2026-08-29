"""Embedded entities -> models.

Two independent sources, merged, best-first:

* **The typed entity graph** (``included[]`` from the embedded Voyager
  envelopes). Complete when present — every position, school, skill and
  certification the viewer may see.
* **JSON-LD** (``schema.org/Person``). A much smaller subset, but LinkedIn emits
  it for search engines, which makes it both stable and present even on
  reduced-visibility pages. Used to fill anything the entity graph didn't give.

Every field read tolerates absence, and both of LinkedIn's date shapes are
accepted: the older ``timePeriod: {startDate, endDate}`` and the newer
``dateRange: {start, end}``. Which one a page carries depends on how recently
that surface was migrated, and it is not worth guessing.
"""

from __future__ import annotations

import logging
from typing import Any

from app.models import (
    CertificationItem,
    EducationItem,
    ExperienceItem,
    LanguageItem,
    LinkedInProfile,
    ProfileImages,
    SkillItem,
)
from app.providers.embedded.extractor import entities_of_type, find_json_ld_person
from app.site_config import EmbeddedConfig, VocabularyConfig
from app.utils.text import is_masked

logger = logging.getLogger(__name__)


# --- helpers ----------------------------------------------------------------


def _clean(value: Any) -> str | None:
    if isinstance(value, dict):
        # LinkedIn wraps localised strings as {"text": "..."}.
        value = value.get("text")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    # A reduced-visibility page can carry LinkedIn's asterisk redactions here too.
    if not stripped or is_masked(stripped):
        return None
    return stripped


def _first(node: Any, *keys: str) -> Any:
    if not isinstance(node, dict):
        return None
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _format_date(node: Any, vocab: VocabularyConfig) -> str | None:
    """``{"month": 1, "year": 2022}`` -> ``"Jan 2022"``; year-only -> ``"2022"``."""
    if not isinstance(node, dict):
        return None
    year = node.get("year")
    if not year:
        return None
    month = node.get("month")
    if isinstance(month, int) and 1 <= month <= len(vocab.month_names):
        return f"{vocab.month_names[month - 1]} {year}"
    return str(year)


def _date_bounds(entity: dict[str, Any], vocab: VocabularyConfig) -> tuple[str | None, str | None, bool]:
    """Accept either date shape; return ``(start, end, is_current)``."""
    period = _first(entity, "dateRange", "timePeriod")
    if not isinstance(period, dict):
        return None, None, False

    start_node = _first(period, "start", "startDate")
    end_node = _first(period, "end", "endDate")

    start = _format_date(start_node, vocab)
    end = _format_date(end_node, vocab)
    is_current = end_node in (None, {}) and start_node is not None
    return start, end, is_current


def _image_url(node: Any) -> str | None:
    """Rebuild a media URL from LinkedIn's split root + artifact representation."""
    seen: set[int] = set()

    def find_vector(candidate: Any) -> dict[str, Any] | None:
        if not isinstance(candidate, dict) or id(candidate) in seen:
            return None
        seen.add(id(candidate))
        if "rootUrl" in candidate and "artifacts" in candidate:
            return candidate
        for value in candidate.values():
            found = find_vector(value)
            if found is not None:
                return found
        return None

    vector = find_vector(node)
    if vector is None:
        return None

    root = vector.get("rootUrl")
    artifacts = vector.get("artifacts")
    if not isinstance(root, str) or not isinstance(artifacts, list):
        return None

    sized = [
        a for a in artifacts if isinstance(a, dict) and a.get("fileIdentifyingUrlPathSegment")
    ]
    if not sized:
        return None
    largest = max(sized, key=lambda a: a.get("width") or 0)
    return f"{root}{largest['fileIdentifyingUrlPathSegment']}"


# --- entity mappers ---------------------------------------------------------


def map_positions(
    entities: list[dict[str, Any]], config: EmbeddedConfig, vocab: VocabularyConfig
) -> list[ExperienceItem]:
    items: list[ExperienceItem] = []
    for entity in entities_of_type(entities, config.entity_types.get("position", "")):
        try:
            start, end, is_current = _date_bounds(entity, vocab)
            items.append(
                ExperienceItem(
                    title=_clean(_first(entity, "title", "positionTitle")),
                    company=_clean(_first(entity, "companyName", "company")),
                    company_url=_clean(entity.get("companyUrl")),
                    employment_type=_clean(entity.get("employmentType")),
                    location=_clean(_first(entity, "locationName", "geoLocationName")),
                    start_date=start,
                    end_date=end or ("Present" if is_current else None),
                    duration=None,  # computed client-side by LinkedIn, never in the payload
                    is_current=is_current,
                    description=_clean(entity.get("description")),
                )
            )
        except Exception:
            logger.debug("skipped an unmappable position", exc_info=True)
    return [item for item in items if item.title or item.company]


def map_educations(
    entities: list[dict[str, Any]], config: EmbeddedConfig, vocab: VocabularyConfig
) -> list[EducationItem]:
    items: list[EducationItem] = []
    for entity in entities_of_type(entities, config.entity_types.get("education", "")):
        try:
            start, end, _ = _date_bounds(entity, vocab)
            items.append(
                EducationItem(
                    school=_clean(_first(entity, "schoolName", "school")),
                    school_url=None,
                    degree=_clean(entity.get("degreeName")),
                    field_of_study=_clean(entity.get("fieldOfStudy")),
                    start_date=start,
                    end_date=end,
                    description=_clean(entity.get("description")),
                )
            )
        except Exception:
            logger.debug("skipped an unmappable education entry", exc_info=True)
    return [item for item in items if item.school or item.degree]


def map_skills(entities: list[dict[str, Any]], config: EmbeddedConfig) -> list[SkillItem]:
    skills: list[SkillItem] = []
    seen: set[str] = set()
    for entity in entities_of_type(entities, config.entity_types.get("skill", "")):
        name = _clean(entity.get("name"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        skills.append(SkillItem(name=name))
    return skills


def map_certifications(
    entities: list[dict[str, Any]], config: EmbeddedConfig, vocab: VocabularyConfig
) -> list[CertificationItem]:
    items: list[CertificationItem] = []
    for entity in entities_of_type(entities, config.entity_types.get("certification", "")):
        try:
            start, end, _ = _date_bounds(entity, vocab)
            items.append(
                CertificationItem(
                    name=_clean(entity.get("name")),
                    issuer=_clean(_first(entity, "authority", "companyName")),
                    issue_date=start,
                    expiration_date=end,
                    credential_id=_clean(entity.get("licenseNumber")),
                    credential_url=_clean(entity.get("url")),
                )
            )
        except Exception:
            logger.debug("skipped an unmappable certification", exc_info=True)
    return [item for item in items if item.name]


def map_languages(
    entities: list[dict[str, Any]], config: EmbeddedConfig, vocab: VocabularyConfig
) -> list[LanguageItem]:
    items: list[LanguageItem] = []
    for entity in entities_of_type(entities, config.entity_types.get("language", "")):
        name = _clean(entity.get("name"))
        if not name:
            continue
        raw = _clean(entity.get("proficiency"))
        items.append(
            LanguageItem(name=name, proficiency=vocab.language_proficiency.get(raw or "", raw))
        )
    return items


# --- JSON-LD fallback -------------------------------------------------------


def _json_ld_fields(person: dict[str, Any] | None) -> dict[str, Any]:
    """The subset schema.org gives us, normalised onto our own field names."""
    if not person:
        return {}

    job_title = person.get("jobTitle")
    if isinstance(job_title, list):
        job_title = job_title[0] if job_title else None

    image = person.get("image")
    image_url = None
    if isinstance(image, dict):
        image_url = image.get("contentUrl") or image.get("url")
    elif isinstance(image, str):
        image_url = image

    address = person.get("address")
    location = None
    if isinstance(address, dict):
        location = ", ".join(
            part
            for part in (
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            )
            if isinstance(part, str) and part
        ) or None

    return {
        "full_name": _clean(person.get("name")),
        "headline": _clean(job_title),
        "about": _clean(person.get("description")),
        "location": location,
        "profile_picture_url": _clean(image_url),
    }


# --- top level --------------------------------------------------------------


def map_profile(
    entities: list[dict[str, Any]],
    blobs: list[Any],
    *,
    public_id: str,
    profile_url: str,
    input_url: str,
    config: EmbeddedConfig,
    vocab: VocabularyConfig,
    source: str = "embedded",
) -> LinkedInProfile:
    profile_entities = entities_of_type(entities, config.entity_types.get("profile", ""))
    # Several Profile entities can appear (the viewer's own, for the nav bar).
    # The one for this page is the one whose publicIdentifier matches the URL.
    profile: dict[str, Any] = {}
    for candidate in profile_entities:
        if _clean(candidate.get("publicIdentifier")) == public_id:
            profile = candidate
            break
    if not profile and profile_entities:
        profile = max(profile_entities, key=len)

    fallback = _json_ld_fields(find_json_ld_person(blobs))

    first_name = _clean(profile.get("firstName"))
    last_name = _clean(profile.get("lastName"))
    full_name = " ".join(p for p in (first_name, last_name) if p) or fallback.get("full_name")

    if not full_name:
        raise ValueError(
            "no profile entity and no JSON-LD Person in the page — LinkedIn "
            "returned a page without profile data"
        )

    if not first_name and full_name:
        first_name, _, last_name = full_name.partition(" ")
        first_name, last_name = _clean(first_name), _clean(last_name)

    experience = map_positions(entities, config, vocab)
    current = next((item for item in experience if item.is_current), None)

    location = (
        _clean(_first(profile, "locationName", "geoLocationName"))
        or _clean(_first(profile.get("geoLocation") or {}, "postalCode"))
        or fallback.get("location")
    )

    return LinkedInProfile(
        input_url=input_url,
        profile_url=profile_url,
        public_id=_clean(profile.get("publicIdentifier")) or public_id,
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        headline=_clean(profile.get("headline")) or fallback.get("headline"),
        location=location,
        about=_clean(_first(profile, "summary", "about")) or fallback.get("about"),
        current_company=current.company if current else None,
        connections=None,  # not embedded in the page payloads
        followers=None,
        images=ProfileImages(
            profile_picture_url=(
                _image_url(profile.get("profilePicture"))
                or fallback.get("profile_picture_url")
            ),
            background_image_url=_image_url(profile.get("backgroundImage")),
        ),
        experience=experience,
        education=map_educations(entities, config, vocab),
        skills=map_skills(entities, config),
        certifications=map_certifications(entities, config, vocab),
        languages=map_languages(entities, config, vocab),
        source=source,
    )
