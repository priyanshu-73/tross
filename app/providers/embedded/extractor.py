"""Pull the JSON LinkedIn embeds in its own profile pages.

linkedin.com server-renders each profile with the data the page was built from
already inlined, so the HTML response *contains* the API payloads. Reading them
out avoids the problem that killed the direct-API provider: you never have to
know which Voyager endpoint is current, because the page URL — the one address
LinkedIn cannot change — carries whatever the page needed.

Three shapes appear in the markup, and all three are collected:

1. ``<code id="bpr-guid-N">`` — the bulk of it. LinkedIn parks each pre-fetched
   API response inside an HTML comment within a ``<code>`` element, so the
   browser doesn't render it. Content is the normalised Voyager envelope:
   ``{"data": {...}, "included": [ ...flat list of typed entities... ]}``.
2. ``<script type="application/ld+json">`` — schema.org JSON-LD emitted for
   search engines. A much smaller subset, but stable and well-specified, which
   makes it a good fallback when the entity graph is absent.
3. ``<script type="application/json">`` — occasional additional state.

Nothing here is LinkedIn-specific beyond those three selectors; the *meaning* of
what comes out is `mapper.py`'s problem.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from bs4 import BeautifulSoup, Comment

logger = logging.getLogger(__name__)

# Entities arrive in a flat list; this is the key that says what each one is.
TYPE_KEY = "$type"


def _loads(raw: str | None) -> Any:
    """Parse a candidate blob, tolerating comment wrappers and junk."""
    if not raw:
        return None
    text = raw.strip()
    if text.startswith("<!--"):
        text = text[4:]
    if text.endswith("-->"):
        text = text[:-3]
    text = text.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _element_text(tag: Any) -> str | None:
    """Text of a tag, reaching inside an HTML comment when that's all there is."""
    comment = tag.find(string=lambda node: isinstance(node, Comment))
    if comment is not None:
        return str(comment)
    return tag.string or tag.get_text()


def iter_json_blobs(html: str) -> list[Any]:
    """Every parseable JSON object embedded in the page, in document order."""
    soup = BeautifulSoup(html, "lxml")
    blobs: list[Any] = []

    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        parsed = _loads(_element_text(tag))
        if parsed is not None:
            blobs.append(parsed)

    for tag in soup.find_all("code"):
        parsed = _loads(_element_text(tag))
        if parsed is not None:
            blobs.append(parsed)

    for tag in soup.find_all("script", attrs={"type": "application/json"}):
        parsed = _loads(_element_text(tag))
        if parsed is not None:
            blobs.append(parsed)

    logger.debug("extracted %d embedded JSON blobs", len(blobs))
    return blobs


def collect_included(blobs: list[Any]) -> list[dict[str, Any]]:
    """Flatten every ``included`` array into one list of typed entities.

    A profile page carries several envelopes (the profile, its positions, the
    viewer's own nav state, ...). Merging them and letting callers filter by
    ``$type`` is far more robust than walking any particular envelope's shape,
    which is what LinkedIn actually changes between releases.
    """
    entities: list[dict[str, Any]] = []
    seen: set[str] = set()

    for blob in blobs:
        if not isinstance(blob, dict):
            continue
        included = blob.get("included")
        if not isinstance(included, list):
            continue
        for entity in included:
            if not isinstance(entity, dict) or TYPE_KEY not in entity:
                continue
            # entityUrn is stable and unique; dedupe on it so entities repeated
            # across envelopes are only mapped once.
            urn = entity.get("entityUrn")
            if isinstance(urn, str):
                if urn in seen:
                    continue
                seen.add(urn)
            entities.append(entity)

    return entities


def entities_of_type(
    entities: list[dict[str, Any]], type_fragment: str
) -> list[dict[str, Any]]:
    """Entities whose ``$type`` *ends with* ``type_fragment``.

    Suffix rather than equality on purpose: LinkedIn versions and renames the
    fully-qualified package path (``com.linkedin.voyager.dash.identity.profile.*``)
    far more often than it renames the leaf class, so matching the tail survives
    churn that matching the whole string would not.

    Anchored to the end rather than matched anywhere, because ``PositionGroup``
    contains ``identity.profile.Position`` — an unanchored match returns every
    role twice.
    """
    if not type_fragment:
        return []
    return [
        entity
        for entity in entities
        if isinstance(entity.get(TYPE_KEY), str)
        and entity[TYPE_KEY].endswith(type_fragment)
    ]


def find_json_ld_person(blobs: list[Any]) -> dict[str, Any] | None:
    """The schema.org ``Person`` node, wherever JSON-LD chose to put it."""

    def walk(node: Any) -> dict[str, Any] | None:
        if isinstance(node, dict):
            if node.get("@type") == "Person":
                return node
            for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                found = walk(node.get(key))
                if found is not None:
                    return found
        elif isinstance(node, list):
            for item in node:
                found = walk(item)
                if found is not None:
                    return found
        return None

    for blob in blobs:
        person = walk(blob)
        if person is not None:
            return person
    return None
