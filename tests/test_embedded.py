"""Extraction and mapping of the JSON LinkedIn embeds in its own profile pages."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.providers.embedded import extractor, mapper
from app.site_config import get_site_config

FIXTURE = Path(__file__).parent / "fixtures" / "embedded_profile.html"


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def config():
    return get_site_config().embedded


@pytest.fixture(scope="module")
def vocab():
    return get_site_config().vocabulary


@pytest.fixture(scope="module")
def blobs(html):
    return extractor.iter_json_blobs(html)


@pytest.fixture(scope="module")
def entities(blobs):
    return extractor.collect_included(blobs)


@pytest.fixture(scope="module")
def profile(entities, blobs, config, vocab):
    return mapper.map_profile(
        entities,
        blobs,
        public_id="ada-lovelace-1a2b3c",
        profile_url="https://www.linkedin.com/in/ada-lovelace-1a2b3c/",
        input_url="https://www.linkedin.com/in/ada-lovelace-1a2b3c",
        config=config,
        vocab=vocab,
    )


# --- extraction -------------------------------------------------------------


def test_json_is_recovered_from_inside_html_comments(blobs):
    """LinkedIn hides each payload in a comment so the browser won't render it."""
    assert len(blobs) >= 5


def test_non_json_code_blocks_are_skipped_silently(html):
    # The fixture contains <code><!--not json--></code>; nothing should raise.
    assert extractor.iter_json_blobs(html)


def test_entities_are_flattened_across_every_envelope(entities):
    types = {e["$type"].rsplit(".", 1)[-1] for e in entities}
    assert {"Profile", "Position", "Education", "Skill", "Certification", "Language"} <= types


def test_entities_repeated_across_envelopes_are_deduped_by_urn(entities):
    skills = extractor.entities_of_type(entities, "identity.profile.Skill")
    urns = [e["entityUrn"] for e in skills]
    assert len(urns) == len(set(urns))


def test_type_matching_is_by_substring_so_package_renames_survive(entities):
    """LinkedIn versions the package path far more often than the leaf class."""
    assert extractor.entities_of_type(entities, "identity.profile.Position")
    assert extractor.entities_of_type(entities, "Position")
    assert extractor.entities_of_type(entities, "com.linkedin.voyager.dash.NOPE") == []


def test_json_ld_person_is_found(blobs):
    person = extractor.find_json_ld_person(blobs)
    assert person is not None
    assert person["name"] == "Ada Lovelace"


def test_extraction_of_empty_html_is_empty_not_an_error():
    assert extractor.iter_json_blobs("<html><body></body></html>") == []
    assert extractor.collect_included([]) == []


# --- mapping ----------------------------------------------------------------


def test_identity(profile):
    assert profile.full_name == "Ada Lovelace"
    assert profile.first_name == "Ada"
    assert profile.last_name == "Lovelace"
    assert profile.headline.startswith("Principal Engineer at Acme Corp")
    assert profile.location == "Bengaluru, Karnataka, India"
    assert profile.about.startswith("Mathematician and engineer.")
    assert profile.source == "embedded"


def test_the_viewers_own_profile_entity_is_not_mistaken_for_the_subject(profile):
    """A page carries the signed-in user's Profile too, for the nav bar."""
    assert profile.public_id == "ada-lovelace-1a2b3c"
    assert profile.first_name != "Viewer"


def test_images_prefer_the_entity_graph_over_json_ld(profile):
    assert "800_800" in profile.images.profile_picture_url
    assert "jsonld-fallback" not in profile.images.profile_picture_url
    assert "1400_350" in profile.images.background_image_url


def test_experience(profile):
    assert [e.title for e in profile.experience] == [
        "Principal Engineer",
        "Senior Software Engineer",
        "Software Engineer",
    ]

    principal = profile.experience[0]
    assert principal.company == "Acme Corp"
    assert principal.employment_type == "Full-time"
    assert principal.start_date == "Jan 2022"
    assert principal.end_date == "Present"
    assert principal.is_current is True
    assert "event-driven ingestion" in principal.description


def test_both_of_linkedins_date_shapes_are_accepted(profile):
    """`dateRange` on the first two, legacy `timePeriod` on the third."""
    assert profile.experience[1].start_date == "Jun 2020"
    assert profile.experience[1].end_date == "Dec 2021"
    assert profile.experience[2].start_date == "Mar 2019"
    assert profile.experience[2].end_date == "May 2020"
    assert profile.experience[2].is_current is False


def test_education_handles_year_only_dates(profile):
    school = profile.education[0]
    assert school.school == "University of Cambridge"
    assert school.degree == "Bachelor of Science - BS"
    assert school.field_of_study == "Mathematics"
    assert school.start_date == "2014"
    assert school.end_date == "2018"


def test_skills_are_deduped(profile):
    assert [s.name for s in profile.skills] == ["Python", "Distributed Systems"]


def test_certifications(profile):
    cert = profile.certifications[0]
    assert cert.name == "AWS Certified Solutions Architect"
    assert cert.issuer == "Amazon Web Services (AWS)"
    assert cert.issue_date == "Mar 2021"
    assert cert.expiration_date == "Mar 2024"
    assert cert.credential_id == "AWS-SAA-99120"


def test_language_enums_become_readable_text(profile):
    assert [(item.name, item.proficiency) for item in profile.languages] == [
        ("English", "Native or bilingual proficiency"),
        ("Hindi", "Professional working proficiency"),
    ]


# --- fallback behaviour -----------------------------------------------------


def test_json_ld_carries_the_profile_when_the_entity_graph_is_absent(html, config, vocab):
    """Reduced-visibility pages often ship JSON-LD and nothing else."""
    blobs = extractor.iter_json_blobs(html)
    result = mapper.map_profile(
        [],  # no entities at all
        blobs,
        public_id="ada-lovelace-1a2b3c",
        profile_url="u",
        input_url="u",
        config=config,
        vocab=vocab,
    )
    assert result.full_name == "Ada Lovelace"
    assert result.headline == "Principal Engineer"
    assert result.location == "Bengaluru, Karnataka, IN"
    assert result.about == "JSON-LD fallback description."
    assert "jsonld-fallback" in result.images.profile_picture_url
    assert result.experience == []


def test_a_page_with_neither_source_is_rejected(config, vocab):
    with pytest.raises(ValueError):
        mapper.map_profile(
            [], [], public_id="x", profile_url="u", input_url="u",
            config=config, vocab=vocab,
        )
