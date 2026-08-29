"""Voyager Dash entities -> models.

Dash responses are *normalised*: a flat ``included[]`` array of entities, each
tagged with a ``$type``, plus a ``data`` envelope of references between them.
That shape is a gift for mapping — filter by type, and never walk a nested tree
that LinkedIn is free to restructure.

Three things about the payload shape the code:

* **Types are matched by substring.** LinkedIn versions the fully-qualified
  package path (``com.linkedin.voyager.dash.identity.profile.*``) far more often
  than it renames the leaf class, so matching the tail survives churn that
  matching the whole string would not. The fragments live in `config.json`.
* **Images are split** into a ``rootUrl`` plus per-size ``artifacts``, wrapped in
  one of several union keys (``displayImageWithFrameReferenceUnion``,
  ``originalImageReference``, ...). Rather than name the wrappers, the vector is
  found by structure: the first nested object carrying both keys.
* **Dates are real**: ``{"month": 10, "year": 2024}``. They are still rendered to
  LinkedIn's display strings (``"Oct 2024"``) so every provider returns one
  contract — see the note in `app/providers/public/mapper.py`.

Pure functions, no HTTP, so all of it is testable against committed fixtures.
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
from app.site_config import VocabularyConfig, VoyagerConfig
from app.utils.text import is_masked

logger = logging.getLogger(__name__)


# --- entity access ----------------------------------------------------------


def included(payload: Any) -> list[dict[str, Any]]:
    """The flat entity list from a normalised Dash response."""
    if not isinstance(payload, dict):
        return []
    items = payload.get("included")
    return [e for e in items if isinstance(e, dict)] if isinstance(items, list) else []


def dedupe(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop entities already seen, keyed by ``entityUrn``.

    The section endpoints re-send entities the profile call already returned, so
    merging their payloads naively doubles every position and certification.
    Entities without a urn are kept as-is: they cannot be compared safely.
    """
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for entity in entities:
        urn = entity.get("entityUrn")
        if isinstance(urn, str):
            if urn in seen:
                continue
            seen.add(urn)
        unique.append(entity)
    return unique


def of_type(entities: list[dict[str, Any]], fragment: str) -> list[dict[str, Any]]:
    """Entities whose ``$type`` *ends with* ``fragment``.

    Anchored to the end rather than matched anywhere, which matters more than it
    looks: ``PositionGroup`` contains ``identity.profile.Position``, so a plain
    substring test silently returns every role twice. Matching the tail still
    tolerates LinkedIn versioning the package path, which is the whole point.
    """
    if not fragment:
        return []
    return [
        e
        for e in entities
        if isinstance(e.get("$type"), str) and e["$type"].endswith(fragment)
    ]


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or is_masked(text):
        return None
    return text


def _first(node: Any, *keys: str) -> Any:
    if not isinstance(node, dict):
        return None
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


# --- value formatting -------------------------------------------------------


def _date(node: Any, vocab: VocabularyConfig) -> str | None:
    if not isinstance(node, dict):
        return None
    year = node.get("year")
    if not year:
        return None
    month = node.get("month")
    if isinstance(month, int) and 1 <= month <= len(vocab.month_names):
        return f"{vocab.month_names[month - 1]} {year}"
    return str(year)


def _date_range(entity: dict[str, Any], vocab: VocabularyConfig) -> tuple[str | None, str | None, bool]:
    period = entity.get("dateRange")
    if not isinstance(period, dict):
        return None, None, False
    start = _date(period.get("start"), vocab)
    end = _date(period.get("end"), vocab)
    # An ongoing role simply has no end.
    return start, end, (period.get("end") in (None, {}) and start is not None)


def _image(node: Any) -> str | None:
    """Find a vector image anywhere under ``node`` and build its largest URL."""
    seen: set[int] = set()

    def find(candidate: Any) -> dict[str, Any] | None:
        if isinstance(candidate, list):
            for item in candidate:
                found = find(item)
                if found is not None:
                    return found
            return None
        if not isinstance(candidate, dict) or id(candidate) in seen:
            return None
        seen.add(id(candidate))
        if "rootUrl" in candidate and "artifacts" in candidate:
            return candidate
        for value in candidate.values():
            found = find(value)
            if found is not None:
                return found
        return None

    vector = find(node)
    if vector is None:
        return None
    root = vector.get("rootUrl")
    artifacts = vector.get("artifacts")
    if not isinstance(root, str) or not isinstance(artifacts, list):
        return None
    sized = [a for a in artifacts if isinstance(a, dict) and a.get("fileIdentifyingUrlPathSegment")]
    if not sized:
        return None
    largest = max(sized, key=lambda a: a.get("width") or 0)
    return f"{root}{largest['fileIdentifyingUrlPathSegment']}"


def _org_index(entities: list[dict[str, Any]], config: VoyagerConfig) -> dict[str, dict[str, Any]]:
    """Companies and schools, keyed by entityUrn, so positions can resolve URLs."""
    index: dict[str, dict[str, Any]] = {}
    for key in ("company", "school"):
        for org in of_type(entities, config.entity_types.get(key, "")):
            urn = org.get("entityUrn")
            if isinstance(urn, str):
                index[urn] = org
    return index


# --- section mappers --------------------------------------------------------


def map_positions(
    entities: list[dict[str, Any]], config: VoyagerConfig, vocab: VocabularyConfig
) -> list[ExperienceItem]:
    orgs = _org_index(entities, config)
    items: list[ExperienceItem] = []

    for entity in of_type(entities, config.entity_types.get("position", "")):
        try:
            start, end, current = _date_range(entity, vocab)
            org = orgs.get(entity.get("companyUrn") or "") or {}
            items.append(
                ExperienceItem(
                    title=_clean(entity.get("title")),
                    company=_clean(entity.get("companyName")) or _clean(org.get("name")),
                    company_url=_clean(org.get("url")),
                    employment_type=_clean(entity.get("employmentTypeName")),
                    location=_clean(entity.get("locationName")),
                    start_date=start,
                    end_date=end or ("Present" if current else None),
                    duration=None,  # LinkedIn computes "4 yrs 2 mos" client-side
                    is_current=current,
                    description=_clean(entity.get("description")),
                )
            )
        except Exception:
            logger.debug("skipped an unmappable position", exc_info=True)
    return [item for item in items if item.title or item.company]


def map_educations(
    entities: list[dict[str, Any]], config: VoyagerConfig, vocab: VocabularyConfig
) -> list[EducationItem]:
    orgs = _org_index(entities, config)
    items: list[EducationItem] = []

    for entity in of_type(entities, config.entity_types.get("education", "")):
        try:
            start, end, _ = _date_range(entity, vocab)
            org = orgs.get(entity.get("schoolUrn") or "") or {}
            items.append(
                EducationItem(
                    school=_clean(entity.get("schoolName")) or _clean(org.get("name")),
                    school_url=_clean(org.get("url")),
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


def map_skills(entities: list[dict[str, Any]], config: VoyagerConfig) -> list[SkillItem]:
    skills: list[SkillItem] = []
    seen: set[str] = set()
    for entity in of_type(entities, config.entity_types.get("skill", "")):
        name = _clean(entity.get("name"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        count = entity.get("endorsementCount")
        skills.append(
            SkillItem(name=name, endorsement_count=count if isinstance(count, int) else None)
        )
    return skills


def map_certifications(
    entities: list[dict[str, Any]], config: VoyagerConfig, vocab: VocabularyConfig
) -> list[CertificationItem]:
    items: list[CertificationItem] = []
    for entity in of_type(entities, config.entity_types.get("certification", "")):
        try:
            start, end, _ = _date_range(entity, vocab)
            items.append(
                CertificationItem(
                    name=_clean(entity.get("name")),
                    issuer=_clean(_first(entity, "authority", "displaySource")),
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
    entities: list[dict[str, Any]], config: VoyagerConfig, vocab: VocabularyConfig
) -> list[LanguageItem]:
    items: list[LanguageItem] = []
    for entity in of_type(entities, config.entity_types.get("language", "")):
        name = _clean(entity.get("name"))
        if not name:
            continue
        raw = _clean(entity.get("proficiency"))
        items.append(
            LanguageItem(name=name, proficiency=vocab.language_proficiency.get(raw or "", raw))
        )
    return items


# --- top level --------------------------------------------------------------


def map_profile(
    entities: list[dict[str, Any]],
    *,
    public_id: str,
    profile_url: str,
    input_url: str,
    config: VoyagerConfig,
    vocab: VocabularyConfig,
    source: str = "voyager",
) -> LinkedInProfile:
    profiles = of_type(entities, config.entity_types.get("profile", ""))
    # A response can carry the viewer's own Profile too; take the one asked for.
    profile: dict[str, Any] = next(
        (p for p in profiles if _clean(p.get("publicIdentifier")) == public_id),
        profiles[0] if profiles else {},
    )

    first_name = _clean(profile.get("firstName"))
    last_name = _clean(profile.get("lastName"))
    full_name = " ".join(p for p in (first_name, last_name) if p) or None
    if not full_name:
        raise ValueError("no Profile entity with a name in the Dash response")

    experience = map_positions(entities, config, vocab)
    current = next((item for item in experience if item.is_current), None)

    geos = of_type(entities, config.entity_types.get("geo", ""))
    location = next(
        (_clean(g.get("defaultLocalizedName")) for g in geos if g.get("defaultLocalizedName")),
        None,
    ) or _clean(_first(profile.get("location") or {}, "countryCode"))

    return LinkedInProfile(
        input_url=input_url,
        profile_url=profile_url,
        public_id=_clean(profile.get("publicIdentifier")) or public_id,
        full_name=full_name,
        first_name=first_name,
        last_name=last_name,
        headline=_clean(profile.get("headline")),
        location=location,
        about=_clean(profile.get("summary")),
        current_company=current.company if current else None,
        connections=None,  # a separate Dash collection; not fetched
        followers=None,
        images=ProfileImages(
            profile_picture_url=_image(profile.get("profilePicture")),
            background_image_url=_image(profile.get("backgroundPicture")),
        ),
        experience=experience,
        education=map_educations(entities, config, vocab),
        skills=map_skills(entities, config),
        certifications=map_certifications(entities, config, vocab),
        languages=map_languages(entities, config, vocab),
        source=source,
    )
