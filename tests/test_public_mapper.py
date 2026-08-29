"""schema.org JSON-LD -> models, for the credential-free provider.

The fixture mirrors the shape LinkedIn actually publishes (verified against a
live public profile), including the guest redaction it sometimes applies.
"""

from __future__ import annotations

import pytest

from app.providers.public import mapper
from app.providers.public.mapper import is_masked

PERSON = {
    "@type": "Person",
    "name": "Ada Lovelace",
    "disambiguatingDescription": "Creator, Top Voice",
    "jobTitle": ["Principal Engineer", "Advisor"],
    "description": "Mathematician and engineer. I build data platforms.",
    "address": {
        "@type": "PostalAddress",
        "addressCountry": "IN",
        "addressLocality": "Bengaluru, Karnataka, India",
    },
    "image": {
        "@type": "ImageObject",
        "contentUrl": "https://media.licdn.com/dms/image/v2/photo.jpg",
    },
    "sameAs": "https://www.linkedin.com/in/ada-lovelace-1a2b3c",
    "worksFor": [
        {
            "@type": "Organization",
            "name": "Acme Corp",
            "url": "https://www.linkedin.com/company/acme",
            "member": {
                "@type": "OrganizationRole",
                "roleName": "Principal Engineer",
                "startDate": "2022-01",
            },
        },
        {
            "@type": "Organization",
            "name": "Globex",
            "url": "https://www.linkedin.com/company/globex",
            "member": [
                {
                    "@type": "OrganizationRole",
                    "roleName": "Software Engineer",
                    "startDate": "2019-03",
                    "endDate": "2020-05",
                }
            ],
        },
    ],
    "alumniOf": [
        {
            "@type": "EducationalOrganization",
            "name": "University of Cambridge",
            "url": "https://www.linkedin.com/school/cambridge",
            "member": {
                "@type": "OrganizationRole",
                "roleName": "Bachelor of Science",
                "startDate": "2014",
                "endDate": "2018",
            },
        }
    ],
    "knowsLanguage": [{"name": "English"}, {"name": "Hindi"}],
    "awards": ["AWS Certified Solutions Architect"],
    "interactionStatistic": {
        "@type": "InteractionCounter",
        "interactionType": "https://schema.org/FollowAction",
        "userInteractionCount": 12431,
    },
}


@pytest.fixture
def profile():
    return mapper.map_profile(
        PERSON,
        public_id="ada-lovelace-1a2b3c",
        profile_url="https://www.linkedin.com/in/ada-lovelace-1a2b3c/",
        input_url="https://www.linkedin.com/in/ada-lovelace-1a2b3c",
    )


# --- guest redaction --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("************ ******", True),
        ("******", True),
        ("*", True),
        ("  ****  ***  ", True),
        ("Acme Corp", False),
        ("A*B", False),
        ("5 * 3 Consulting", False),
        ("", False),
    ],
)
def test_masked_value_detection(text, expected):
    assert is_masked(text) is expected


def test_redacted_fields_become_absent_not_asterisks():
    """A redaction is missing data, not a value - it must never reach output."""
    redacted = dict(PERSON)
    redacted["jobTitle"] = ["**********"]
    redacted["worksFor"] = [
        {"@type": "Organization", "name": "************ ******", "member": {}}
    ]

    result = mapper.map_profile(
        redacted, public_id="x", profile_url="u", input_url="u"
    )
    assert result.headline == "Creator, Top Voice"  # falls back, not "**********"
    assert result.experience == []
    assert "*" not in (result.headline or "")


# --- mapping ----------------------------------------------------------------


def test_identity(profile):
    assert profile.full_name == "Ada Lovelace"
    assert profile.first_name == "Ada"
    assert profile.last_name == "Lovelace"
    assert profile.headline == "Principal Engineer"
    assert profile.location == "Bengaluru, Karnataka, India"
    assert profile.about.startswith("Mathematician and engineer.")
    assert profile.source == "public"


def test_follower_count_is_formatted(profile):
    assert profile.followers == "12,431"


def test_connections_are_never_published_to_guests(profile):
    assert profile.connections is None


def test_image(profile):
    assert profile.images.profile_picture_url.endswith("photo.jpg")


def test_experience_from_nested_organization_roles(profile):
    assert [(e.title, e.company) for e in profile.experience] == [
        ("Principal Engineer", "Acme Corp"),
        ("Software Engineer", "Globex"),
    ]

    current = profile.experience[0]
    assert current.start_date == "Jan 2022"
    assert current.end_date == "Present"
    assert current.is_current is True

    past = profile.experience[1]
    assert past.start_date == "Mar 2019"
    assert past.end_date == "May 2020"
    assert past.is_current is False


def test_a_single_role_dict_and_a_list_are_both_accepted(profile):
    """`member` arrives as an object for one role and a list for several."""
    assert len(profile.experience) == 2


def test_current_company(profile):
    assert profile.current_company == "Acme Corp"


def test_education(profile):
    school = profile.education[0]
    assert school.school == "University of Cambridge"
    assert school.degree == "Bachelor of Science"
    assert school.start_date == "2014"
    assert school.end_date == "2018"


def test_languages_and_awards(profile):
    assert [item.name for item in profile.languages] == ["English", "Hindi"]
    assert profile.certifications[0].name == "AWS Certified Solutions Architect"


def test_skills_are_never_available_anonymously(profile):
    assert profile.skills == []


def test_an_employer_with_no_role_still_appears():
    person = {
        "name": "Nobody Here",
        "worksFor": [{"@type": "Organization", "name": "Acme Corp"}],
    }
    result = mapper.map_profile(person, public_id="x", profile_url="u", input_url="u")
    assert result.experience[0].company == "Acme Corp"
    assert result.experience[0].title is None


def test_a_sparse_record_yields_nulls_not_errors():
    result = mapper.map_profile(
        {"name": "Nobody Here"}, public_id="x", profile_url="u", input_url="u"
    )
    assert result.full_name == "Nobody Here"
    assert result.experience == []
    assert result.about is None
    assert result.followers is None


def test_a_record_without_a_name_is_rejected():
    with pytest.raises(ValueError):
        mapper.map_profile({"jobTitle": "x"}, public_id="x", profile_url="u", input_url="u")
