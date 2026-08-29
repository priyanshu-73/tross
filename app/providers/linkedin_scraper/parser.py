"""HTML -> Pydantic models.

Kept deliberately free of Playwright so it can be unit-tested against saved
fixtures without a browser or a LinkedIn session.

LinkedIn ships obfuscated, frequently-rotated CSS class names, so the strategy
here is *structural* rather than selector-exact:

* Sections are located via their stable anchor ids (``div#experience``,
  ``div#education``, ...) which exist so LinkedIn's own in-page navigation can
  jump to them.
* Inside a section, every entry is an ``<li>`` whose visible text lives in
  ``<span aria-hidden="true">`` elements (LinkedIn duplicates each string into a
  ``visually-hidden`` sibling for screen readers - reading only the aria-hidden
  copy avoids doubling everything).
* Those spans come out as an ordered list of lines, and the lines are then
  classified by *content* (does it look like a date range? an employment type?)
  instead of by position alone.

Every extractor is defensive: a section that fails to parse yields an empty list
rather than failing the whole request.
"""

from __future__ import annotations

import copy
import logging
import re

from bs4 import BeautifulSoup, Tag

from app.models import (
    CertificationItem,
    EducationItem,
    ExperienceItem,
    LanguageItem,
    LinkedInProfile,
    ProfileImages,
    SkillItem,
)
from app.site_config import get_site_config

logger = logging.getLogger(__name__)

# --- Content classifiers ----------------------------------------------------

_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_PRESENT_RE = re.compile(r"\bpresent\b", re.I)
_DURATION_RE = re.compile(r"\b\d+\s*(?:yr|yrs|year|years|mo|mos|month|months)\b", re.I)
_RANGE_SPLIT_RE = re.compile(r"\s*[-–—]\s*")
_DOT_SPLIT_RE = re.compile(r"\s*·\s*")
_ENDORSEMENT_RE = re.compile(r"([\d,]+)\s+endorsement", re.I)
_CREDENTIAL_ID_RE = re.compile(r"credential\s*id\s*[:\s]\s*(\S+)", re.I)
_COUNT_RE = re.compile(r"([\d,\.]+\+?|\d+[KM]\+?)\s+(followers?|connections?)", re.I)

_EMPLOYMENT_TYPES = {
    "full-time",
    "part-time",
    "self-employed",
    "freelance",
    "contract",
    "internship",
    "apprenticeship",
    "seasonal",
    "temporary",
    "permanent",
}

_SECTION_ANCHORS = {
    "experience": ("experience",),
    "education": ("education",),
    "skills": ("skills",),
    "certifications": ("licenses_and_certifications", "certifications"),
    "languages": ("languages",),
    "about": ("about",),
}


# --- Small helpers ----------------------------------------------------------


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed or None


def _looks_like_dates(line: str) -> bool:
    """True for lines such as '2016 - 2020' or 'Jan 2020 - Present . 4 yrs'."""
    if not line:
        return False
    head = _DOT_SPLIT_RE.split(line)[0]
    return bool(_YEAR_RE.search(head) or _PRESENT_RE.search(head))


def _looks_like_employment(line: str) -> bool:
    first = _DOT_SPLIT_RE.split(line)[0].strip().lower()
    return first in _EMPLOYMENT_TYPES


def _split_date_line(line: str) -> tuple[str | None, str | None, str | None]:
    """'Jan 2020 - Present . 4 yrs 2 mos' -> ('Jan 2020', 'Present', '4 yrs 2 mos')."""
    parts = [p.strip() for p in _DOT_SPLIT_RE.split(line) if p.strip()]
    if not parts:
        return None, None, None

    range_part = parts[0]
    duration = next((p for p in parts[1:] if _DURATION_RE.search(p)), None)

    bounds = _RANGE_SPLIT_RE.split(range_part)
    start = _clean(bounds[0])
    end = _clean(bounds[1]) if len(bounds) > 1 else None

    # A single-token line that is only a duration ('4 yrs') is not a range.
    if start and not end and _DURATION_RE.fullmatch(start.strip()):
        return None, None, start
    return start, end, duration


def _aria_lines(scope: Tag) -> list[str]:
    """Ordered, de-duplicated visible strings inside ``scope``."""
    seen: set[str] = set()
    lines: list[str] = []
    for span in scope.select('span[aria-hidden="true"]'):
        text = _clean(span.get_text(" ", strip=True))
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(text)
    return lines


def _abs_url(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if href.startswith("http"):
        return href.split("?")[0]
    if href.startswith("/"):
        base = get_site_config().linkedin.base_url.rstrip("/")
        return base + href.split("?")[0]
    return None


# --- Entity (list item) decomposition ---------------------------------------


class Entity:
    """One profile list item, split into its headline text and sub-components."""

    __slots__ = ("bold", "lines", "link", "description", "sub_text", "children", "node")

    def __init__(self, li: Tag) -> None:
        self.node = li
        clone = copy.copy(li)

        sub = clone.select_one(".pvs-entity__sub-components")
        sub_clone = copy.copy(sub) if sub is not None else None
        if sub is not None:
            sub.extract()  # keep sub-component text out of the headline lines

        lines = _aria_lines(clone)

        bold_node = clone.select_one('[class*="t-bold"] span[aria-hidden="true"]')
        self.bold = _clean(bold_node.get_text(" ", strip=True)) if bold_node else None
        if self.bold is None and lines:
            self.bold = lines[0]
        self.lines = [line for line in lines if line != self.bold]

        anchor = clone.select_one("a[href]")
        self.link = _abs_url(anchor.get("href") if anchor else None)

        self.children: list[Tag] = []
        self.description: str | None = None
        self.sub_text: str = ""
        if sub_clone is not None:
            self.children = [
                child
                for child in sub_clone.select(":scope > ul > li")
                if child.select_one('[class*="t-bold"]')
            ]
            self.sub_text = sub_clone.get_text(" ", strip=True)
            self.description = self._extract_description(copy.copy(sub_clone))

    @staticmethod
    def _extract_description(sub: Tag) -> str | None:
        # Drop nested entities (grouped roles) so only free text remains.
        for nested in sub.select(":scope > ul > li"):
            if nested.select_one('[class*="t-bold"]'):
                nested.extract()

        chunks = [
            _clean(node.get_text(" ", strip=True))
            for node in sub.select('.inline-show-more-text span[aria-hidden="true"]')
        ]
        chunks = [c for c in chunks if c and not _ENDORSEMENT_RE.search(c)]
        if not chunks:
            chunks = [c for c in _aria_lines(sub) if not _ENDORSEMENT_RE.search(c)]
        if not chunks:
            return None
        # The longest chunk is the description; shorter siblings are captions.
        return max(chunks, key=len)


def _find_section(soup: BeautifulSoup, key: str) -> Tag | None:
    for anchor_id in _SECTION_ANCHORS.get(key, ()):
        anchor = soup.find(id=anchor_id)
        if anchor is None:
            continue
        section = anchor.find_parent("section")
        if section is not None:
            return section
    return None


def _section_entities(section: Tag | None) -> list[Entity]:
    if section is None:
        return []
    items = section.select("ul > li.artdeco-list__item")
    if not items:
        items = [li for li in section.select("ul > li") if li.select_one('[class*="t-bold"]')]
    return [Entity(li) for li in items]


def _detail_page_entities(soup: BeautifulSoup) -> list[Entity]:
    """Entities on a /details/<section>/ page, which has no anchor divs."""
    main = soup.select_one("main") or soup
    items = main.select("li.pvs-list__paged-list-item")
    if not items:
        items = main.select(".pvs-list__container li.artdeco-list__item")
    if not items:
        items = [li for li in main.select("ul > li") if li.select_one('[class*="t-bold"]')]
    return [Entity(li) for li in items]


def _entities_for(html: str, key: str, detail_page: bool) -> list[Entity]:
    soup = BeautifulSoup(html, "lxml")
    if detail_page:
        return _detail_page_entities(soup)
    return _section_entities(_find_section(soup, key))


def _safe(fn, entity):
    try:
        return fn(entity)
    except Exception:
        logger.debug("failed to parse an entry with %s", getattr(fn, "__name__", fn), exc_info=True)
        return None


# --- Section builders -------------------------------------------------------


def _build_experience(entity: Entity) -> list[ExperienceItem]:
    """Return one item, or several when roles are grouped under a company."""
    if entity.children:
        # The company header carries facts that apply to every nested role but
        # are printed only once ("Acme Corp", "Full-time", the office location).
        company = entity.bold
        company_url = entity.link
        parent_employment = next(
            (
                _clean(_DOT_SPLIT_RE.split(line)[0])
                for line in entity.lines
                if _looks_like_employment(line)
            ),
            None,
        )
        parent_location = next(
            (
                line
                for line in entity.lines
                if not _looks_like_dates(line) and not _looks_like_employment(line)
            ),
            None,
        )

        roles: list[ExperienceItem] = []
        for child in entity.children:
            item = _experience_from_lines(Entity(child), company=company)
            item.company_url = item.company_url or company_url
            item.employment_type = item.employment_type or parent_employment
            item.location = item.location or parent_location
            roles.append(item)
        return [r for r in roles if r.title or r.company]

    item = _experience_from_lines(entity)
    return [item] if (item.title or item.company) else []


def _experience_from_lines(entity: Entity, company: str | None = None) -> ExperienceItem:
    title = entity.bold
    lines = list(entity.lines)

    date_idx = next((i for i, line in enumerate(lines) if _looks_like_dates(line)), None)
    start = end = duration = None
    if date_idx is not None:
        start, end, duration = _split_date_line(lines[date_idx])

    employment_type = None
    resolved_company = company
    location = None

    head_lines = lines[:date_idx] if date_idx is not None else lines
    tail_lines = lines[date_idx + 1 :] if date_idx is not None else []

    for line in head_lines:
        parts = [p.strip() for p in _DOT_SPLIT_RE.split(line) if p.strip()]
        if _looks_like_employment(line):
            employment_type = employment_type or _clean(parts[0])
            if not duration:
                duration = next((p for p in parts[1:] if _DURATION_RE.search(p)), None)
        elif resolved_company is None:
            resolved_company = _clean(parts[0])
            for extra in parts[1:]:
                if extra.lower() in _EMPLOYMENT_TYPES:
                    employment_type = employment_type or extra

    for line in tail_lines:
        if _DURATION_RE.fullmatch(line.strip()):
            duration = duration or line.strip()
            continue
        if location is None:
            location = line
            break

    is_current = bool(end and _PRESENT_RE.search(end))

    return ExperienceItem(
        title=title,
        company=resolved_company,
        company_url=entity.link if entity.link and "/company/" in entity.link else None,
        employment_type=employment_type,
        location=location,
        start_date=start,
        end_date=end,
        duration=duration,
        is_current=is_current,
        description=entity.description,
    )


def _build_education(entity: Entity) -> EducationItem | None:
    school = entity.bold
    lines = list(entity.lines)

    date_idx = next((i for i, line in enumerate(lines) if _looks_like_dates(line)), None)
    start = end = None
    if date_idx is not None:
        start, end, _ = _split_date_line(lines[date_idx])
        lines.pop(date_idx)

    degree = field = None
    if lines:
        parts = [p.strip() for p in lines[0].split(",") if p.strip()]
        if parts:
            degree = parts[0]
            field = ", ".join(parts[1:]) or None

    if not (school or degree):
        return None

    link = entity.link
    return EducationItem(
        school=school,
        school_url=link if link and ("/school/" in link or "/company/" in link) else None,
        degree=degree,
        field_of_study=field,
        start_date=start,
        end_date=end,
        description=entity.description,
    )


def _build_skill(entity: Entity) -> SkillItem | None:
    name = entity.bold
    if not name:
        return None
    count = None
    match = _ENDORSEMENT_RE.search(entity.sub_text or "")
    if match:
        try:
            count = int(match.group(1).replace(",", ""))
        except ValueError:
            count = None
    return SkillItem(name=name, endorsement_count=count)


def _build_certification(entity: Entity) -> CertificationItem | None:
    name = entity.bold
    if not name:
        return None

    issuer = None
    issue_date = expiry = None
    for line in entity.lines:
        low = line.lower()
        if low.startswith(("issued", "expire")) or _looks_like_dates(line):
            for part in (p.strip() for p in _DOT_SPLIT_RE.split(line)):
                plow = part.lower()
                if plow.startswith("issued"):
                    issue_date = _clean(part[len("issued") :])
                elif plow.startswith("expire"):
                    expiry = _clean(re.sub(r"^expires?\b", "", part, flags=re.I))
                elif issue_date is None and _looks_like_dates(part):
                    issue_date = _clean(part)
        elif issuer is None:
            issuer = line

    credential_id = None
    id_match = _CREDENTIAL_ID_RE.search(entity.sub_text or "")
    if id_match:
        credential_id = id_match.group(1)

    credential_url = None
    for anchor in entity.node.select("a[href]"):
        if "credential" in anchor.get_text(" ", strip=True).lower():
            credential_url = _abs_url(anchor.get("href"))
            break

    return CertificationItem(
        name=name,
        issuer=issuer,
        issue_date=issue_date,
        expiration_date=expiry,
        credential_id=credential_id,
        credential_url=credential_url,
    )


def _build_language(entity: Entity) -> LanguageItem | None:
    if not entity.bold:
        return None
    return LanguageItem(name=entity.bold, proficiency=entity.lines[0] if entity.lines else None)


# --- Public parse entry points ----------------------------------------------


def parse_experience(html: str, *, detail_page: bool = False) -> list[ExperienceItem]:
    items: list[ExperienceItem] = []
    for entity in _entities_for(html, "experience", detail_page):
        result = _safe(_build_experience, entity)
        if result:
            items.extend(result)
    return items


def parse_education(html: str, *, detail_page: bool = False) -> list[EducationItem]:
    entities = _entities_for(html, "education", detail_page)
    return [item for item in (_safe(_build_education, e) for e in entities) if item]


def parse_skills(html: str, *, detail_page: bool = False) -> list[SkillItem]:
    seen: set[str] = set()
    skills: list[SkillItem] = []
    for entity in _entities_for(html, "skills", detail_page):
        skill = _safe(_build_skill, entity)
        if skill and skill.name.lower() not in seen:
            seen.add(skill.name.lower())
            skills.append(skill)
    return skills


def parse_certifications(html: str, *, detail_page: bool = False) -> list[CertificationItem]:
    entities = _entities_for(html, "certifications", detail_page)
    return [item for item in (_safe(_build_certification, e) for e in entities) if item]


def parse_languages(html: str, *, detail_page: bool = False) -> list[LanguageItem]:
    entities = _entities_for(html, "languages", detail_page)
    return [item for item in (_safe(_build_language, e) for e in entities) if item]


def parse_about(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    section = _find_section(soup, "about")
    if section is None:
        return None
    node = section.select_one('.inline-show-more-text span[aria-hidden="true"]')
    if node is not None:
        return _clean(node.get_text("\n", strip=True))
    lines = [line for line in _aria_lines(section) if line.lower() != "about"]
    return max(lines, key=len) if lines else None


def parse_top_card(soup: BeautifulSoup) -> dict[str, str | None]:
    heading = soup.select_one("main h1") or soup.select_one("h1")
    top = heading.find_parent("section") if heading else None
    scope = top or soup.select_one("main") or soup

    name = _clean(heading.get_text(" ", strip=True)) if heading else None

    headline_node = scope.select_one('div[class*="text-body-medium"]')
    headline = _clean(headline_node.get_text(" ", strip=True)) if headline_node else None

    location = None
    for node in scope.select('span[class*="text-body-small"]'):
        classes = " ".join(node.get("class") or [])
        if "t-black--light" not in classes:
            continue
        text = _clean(node.get_text(" ", strip=True))
        if not text or "contact info" in text.lower() or _COUNT_RE.search(text):
            continue
        location = text
        break

    connections = followers = None
    for match in _COUNT_RE.finditer(scope.get_text(" ", strip=True)):
        value, kind = match.group(1), match.group(2).lower()
        if kind.startswith("follower") and followers is None:
            followers = value
        elif kind.startswith("connection") and connections is None:
            connections = value

    return {
        "full_name": name,
        "headline": headline,
        "location": location,
        "connections": connections,
        "followers": followers,
    }


def parse_images(soup: BeautifulSoup, full_name: str | None) -> ProfileImages:
    picture = None
    for selector in (
        'img[class*="pv-top-card-profile-picture__image"]',
        'img[class*="profile-photo-edit__preview"]',
        'button[aria-label*="photo"] img',
    ):
        node = soup.select_one(selector)
        src = node.get("src") if node else None
        if src and src.startswith("http"):
            picture = src
            break
    if picture is None and full_name:
        first = full_name.split(" ")[0].lower()
        for node in soup.select("main img[src]"):
            src = node.get("src") or ""
            alt = (node.get("alt") or "").lower()
            if src.startswith("http") and first and first in alt:
                picture = src
                break

    background = None
    for selector in (
        'img[class*="profile-background-image"]',
        ".profile-background-image img",
    ):
        node = soup.select_one(selector)
        src = node.get("src") if node else None
        if src and src.startswith("http"):
            background = src
            break

    return ProfileImages(profile_picture_url=picture, background_image_url=background)


def parse_profile(
    html: str,
    *,
    public_id: str,
    profile_url: str,
    input_url: str,
    source: str = "linkedin_scraper",
) -> LinkedInProfile:
    """Parse a full ``/in/<public_id>/`` page into a profile model."""
    soup = BeautifulSoup(html, "lxml")
    top = parse_top_card(soup)

    full_name = top["full_name"]
    if not full_name:
        raise ValueError("no <h1> on the page - not a rendered profile")

    first_name, _, last_name = full_name.partition(" ")

    experience = parse_experience(html)
    current = next((e for e in experience if e.is_current), None)

    return LinkedInProfile(
        input_url=input_url,
        profile_url=profile_url,
        public_id=public_id,
        full_name=full_name,
        first_name=_clean(first_name),
        last_name=_clean(last_name),
        headline=top["headline"],
        location=top["location"],
        about=parse_about(html),
        current_company=current.company if current else None,
        connections=top["connections"],
        followers=top["followers"],
        images=parse_images(soup, full_name),
        experience=experience,
        education=parse_education(html),
        skills=parse_skills(html),
        certifications=parse_certifications(html),
        languages=parse_languages(html),
        source=source,
    )
