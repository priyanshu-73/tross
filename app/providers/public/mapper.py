"""schema.org JSON-LD -> models.

LinkedIn publishes a `Person` record in every public profile page so search
engines can index it. It is the only structured profile data the site gives away
without a session, and it is considerably richer than it first looks:

    name, description, jobTitle, address, image, sameAs,
    worksFor    -> employers, each with a nested OrganizationRole
                   carrying roleName / startDate / endDate
    alumniOf    -> schools, same nested shape
    knowsLanguage, awards,
    interactionStatistic -> follower count

Being a published standard rather than an internal payload, it is the most
stable surface in this whole project — LinkedIn changes it only when it wants to
change what Google sees.

What it cannot give: skills, certifications, connection counts, and per-role
descriptions. Those exist only behind a session.
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
)
from app.utils.text import is_masked

logger = logging.getLogger(__name__)


def _clean(value: Any) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or is_masked(stripped):
        return None
    return stripped


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _roles(node: dict[str, Any]) -> list[dict[str, Any]]:
    """The OrganizationRole entries nested under a worksFor / alumniOf item."""
    return [role for role in _as_list(node.get("member")) if isinstance(role, dict)]


def _year(value: Any) -> str | None:
    """JSON-LD dates are ISO-ish ('2020', '2020-06', '2020-06-01')."""
    text = _clean(value)
    if not text:
        return None
    parts = text.split("-")
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        months = [
            "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
        ]
        index = int(parts[1])
        if 1 <= index <= 12:
            return f"{months[index - 1]} {parts[0]}"
    return parts[0] if parts[0].isdigit() else text


def map_experience(person: dict[str, Any]) -> list[ExperienceItem]:
    items: list[ExperienceItem] = []
    for org in _as_list(person.get("worksFor")):
        if not isinstance(org, dict):
            continue
        company = _clean(org.get("name"))
        company_url = _clean(org.get("url"))
        roles = _roles(org)

        if not roles:
            if company:
                items.append(
                    ExperienceItem(company=company, company_url=company_url, is_current=True)
                )
            continue

        for role in roles:
            start = _year(role.get("startDate"))
            end = _year(role.get("endDate"))
            items.append(
                ExperienceItem(
                    title=_clean(role.get("roleName")),
                    company=company,
                    company_url=company_url,
                    start_date=start,
                    end_date=end or ("Present" if start else None),
                    is_current=end is None,
                )
            )
    return [item for item in items if item.title or item.company]


def map_education(person: dict[str, Any]) -> list[EducationItem]:
    items: list[EducationItem] = []
    for org in _as_list(person.get("alumniOf")):
        if not isinstance(org, dict):
            continue
        school = _clean(org.get("name"))
        school_url = _clean(org.get("url"))
        roles = _roles(org)

        if not roles:
            if school:
                items.append(EducationItem(school=school, school_url=school_url))
            continue

        for role in roles:
            items.append(
                EducationItem(
                    school=school,
                    school_url=school_url,
                    degree=_clean(role.get("roleName")),
                    start_date=_year(role.get("startDate")),
                    end_date=_year(role.get("endDate")),
                )
            )
    return [item for item in items if item.school]


def map_languages(person: dict[str, Any]) -> list[LanguageItem]:
    languages: list[LanguageItem] = []
    for entry in _as_list(person.get("knowsLanguage")):
        name = _clean(entry.get("name") if isinstance(entry, dict) else entry)
        if name:
            languages.append(LanguageItem(name=name))
    return languages


def map_certifications(person: dict[str, Any]) -> list[CertificationItem]:
    """`awards` is the closest thing JSON-LD carries to certifications."""
    items: list[CertificationItem] = []
    for entry in _as_list(person.get("awards")):
        name = _clean(entry.get("name") if isinstance(entry, dict) else entry)
        if name:
            items.append(CertificationItem(name=name))
    return items


def _location(person: dict[str, Any]) -> str | None:
    address = person.get("address")
    if not isinstance(address, dict):
        return _clean(address)
    locality = _clean(address.get("addressLocality"))
    if locality:
        return locality
    return ", ".join(
        part
        for part in (
            _clean(address.get("addressRegion")),
            _clean(address.get("addressCountry")),
        )
        if part
    ) or None


def _followers(person: dict[str, Any]) -> str | None:
    for stat in _as_list(person.get("interactionStatistic")):
        if not isinstance(stat, dict):
            continue
        count = stat.get("userInteractionCount")
        if isinstance(count, int):
            return f"{count:,}"
    return None


def _image(person: dict[str, Any]) -> str | None:
    image = person.get("image")
    if isinstance(image, dict):
        return _clean(image.get("contentUrl") or image.get("url"))
    return _clean(image)


def map_profile(
    person: dict[str, Any],
    *,
    public_id: str,
    profile_url: str,
    input_url: str,
    source: str = "public",
) -> LinkedInProfile:
    full_name = _clean(person.get("name"))
    if not full_name:
        raise ValueError("JSON-LD Person carries no usable name")

    first_name, _, last_name = full_name.partition(" ")
    experience = map_experience(person)
    current = next((item for item in experience if item.is_current), None)

    # jobTitle is a list of every current role; the headline reads best as the
    # first, and `disambiguatingDescription` is LinkedIn's own subtitle.
    headline = _clean(person.get("jobTitle")) or _clean(
        person.get("disambiguatingDescription")
    )

    return LinkedInProfile(
        input_url=input_url,
        profile_url=profile_url,
        public_id=public_id,
        full_name=full_name,
        first_name=_clean(first_name),
        last_name=_clean(last_name),
        headline=headline,
        location=_location(person),
        about=_clean(person.get("description")),
        current_company=current.company if current else None,
        connections=None,  # never published to guests
        followers=_followers(person),
        images=ProfileImages(profile_picture_url=_image(person)),
        experience=experience,
        education=map_education(person),
        skills=[],  # not in JSON-LD at any visibility
        certifications=map_certifications(person),
        languages=map_languages(person),
        source=source,
    )
