"""Voyager Dash entities -> models.

The fixture mirrors a real `FullProfileWithEntities` response, including the two
traps that response actually contains: a `PositionGroup` alongside `Position`,
and the viewer's own `Profile` alongside the requested one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.providers.voyager import mapper
from app.site_config import get_site_config

FIXTURE = Path(__file__).parent / "fixtures" / "voyager_dash.json"


@pytest.fixture(scope="module")
def config():
    return get_site_config().voyager


@pytest.fixture(scope="module")
def vocab():
    return get_site_config().vocabulary


@pytest.fixture(scope="module")
def entities():
    return mapper.included(json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture(scope="module")
def profile(entities, config, vocab):
    return mapper.map_profile(
        entities,
        public_id="ada-lovelace-1a2b3c",
        profile_url="https://www.linkedin.com/in/ada-lovelace-1a2b3c/",
        input_url="https://www.linkedin.com/in/ada-lovelace-1a2b3c",
        config=config,
        vocab=vocab,
    )


# --- entity selection -------------------------------------------------------


def test_type_matching_is_anchored_to_the_end(entities, config):
    """`PositionGroup` ends with a superstring of `Position`.

    An unanchored substring match returns every role twice - which it did, until
    a live response showed 16 positions where there were 8.
    """
    positions = mapper.of_type(entities, config.entity_types["position"])
    assert len(positions) == 2
    assert all(e["$type"].endswith("Position") for e in positions)

    groups = mapper.of_type(entities, "PositionGroup")
    assert len(groups) == 1


def test_dedupe_drops_entities_repeated_across_responses():
    """Section endpoints re-send entities the profile call already returned."""
    duplicated = [
        {"$type": "X", "entityUrn": "urn:a", "n": 1},
        {"$type": "X", "entityUrn": "urn:a", "n": 2},
        {"$type": "X", "entityUrn": "urn:b"},
        {"$type": "X"},  # no urn: cannot be compared, so kept
        {"$type": "X"},
    ]
    result = mapper.dedupe(duplicated)
    assert [e.get("entityUrn") for e in result] == ["urn:a", "urn:b", None, None]
    assert result[0]["n"] == 1, "the first occurrence wins"


def test_the_viewers_own_profile_is_not_mistaken_for_the_subject(profile):
    assert profile.public_id == "ada-lovelace-1a2b3c"
    assert profile.first_name == "Ada"


def test_included_of_a_malformed_payload_is_empty():
    assert mapper.included(None) == []
    assert mapper.included({"data": {}}) == []
    assert mapper.included({"included": "nope"}) == []


# --- mapping ----------------------------------------------------------------


def test_identity(profile):
    assert profile.full_name == "Ada Lovelace"
    assert profile.headline.startswith("Principal Engineer at Acme Corp")
    assert profile.about == "Mathematician and engineer. I build data platforms."
    assert profile.source == "voyager"


def test_location_prefers_the_resolved_geo_over_a_country_code(profile):
    assert profile.location == "Bengaluru, Karnataka, India"


def test_images_pick_the_largest_artifact_through_either_union_wrapper(profile):
    assert "800_800" in profile.images.profile_picture_url
    assert "1400_350" in profile.images.background_image_url


def test_experience(profile):
    assert [e.title for e in profile.experience] == [
        "Principal Engineer",
        "Software Engineer",
    ]

    current = profile.experience[0]
    assert current.company == "Acme Corp"
    assert current.employment_type == "Full-time"
    assert current.location == "Bengaluru, Karnataka, India"
    assert current.start_date == "Jan 2022"
    assert current.end_date == "Present"
    assert current.is_current is True
    assert "migration" in current.description
    # resolved through the Company entity referenced by companyUrn
    assert current.company_url == "https://www.linkedin.com/company/acme/"

    past = profile.experience[1]
    assert past.start_date == "Mar 2019"
    assert past.end_date == "May 2020"
    assert past.is_current is False
    assert past.company_url is None


def test_current_company(profile):
    assert profile.current_company == "Acme Corp"


def test_education_resolves_its_school_and_year_only_dates(profile):
    school = profile.education[0]
    assert school.school == "University of Cambridge"
    assert school.school_url == "https://www.linkedin.com/school/cambridge/"
    assert school.degree == "Bachelor of Science - BS"
    assert school.field_of_study == "Mathematics"
    assert school.start_date == "2014"
    assert school.end_date == "2018"


def test_skills(profile):
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
        ("English", "Native or bilingual proficiency")
    ]


# --- resilience -------------------------------------------------------------


def test_a_sparse_profile_yields_nulls_not_errors(config, vocab):
    minimal = [
        {
            "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
            "entityUrn": "urn:li:fsd_profile:X",
            "publicIdentifier": "nobody",
            "firstName": "Nobody",
            "lastName": "Here",
        }
    ]
    result = mapper.map_profile(
        minimal, public_id="nobody", profile_url="u", input_url="u",
        config=config, vocab=vocab,
    )
    assert result.full_name == "Nobody Here"
    assert result.experience == []
    assert result.skills == []
    assert result.about is None
    assert result.images.profile_picture_url is None


def test_a_response_with_no_named_profile_is_rejected(config, vocab):
    with pytest.raises(ValueError):
        mapper.map_profile(
            [{"$type": "com.linkedin.voyager.dash.common.Geo", "entityUrn": "urn:g"}],
            public_id="x", profile_url="u", input_url="u",
            config=config, vocab=vocab,
        )


def test_redacted_values_do_not_reach_the_output(config, vocab):
    redacted = [
        {
            "$type": "com.linkedin.voyager.dash.identity.profile.Profile",
            "entityUrn": "urn:li:fsd_profile:X",
            "publicIdentifier": "nobody",
            "firstName": "Nobody",
            "lastName": "Here",
            "headline": "**********",
        }
    ]
    result = mapper.map_profile(
        redacted, public_id="nobody", profile_url="u", input_url="u",
        config=config, vocab=vocab,
    )
    assert result.headline is None
