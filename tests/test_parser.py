from pathlib import Path

import pytest

from app.providers.linkedin_scraper import parser

FIXTURE = Path(__file__).parent / "fixtures" / "profile.html"


@pytest.fixture(scope="module")
def html() -> str:
    return FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def profile(html):
    return parser.parse_profile(
        html,
        public_id="ada-lovelace-1a2b3c",
        profile_url="https://www.linkedin.com/in/ada-lovelace-1a2b3c/",
        input_url="https://www.linkedin.com/in/ada-lovelace-1a2b3c",
    )


def test_top_card(profile):
    assert profile.full_name == "Ada Lovelace"
    assert profile.first_name == "Ada"
    assert profile.last_name == "Lovelace"
    assert profile.headline.startswith("Principal Engineer at Acme Corp")
    assert profile.location == "Bengaluru, Karnataka, India"
    assert profile.followers == "12,431"
    assert profile.connections == "500+"


def test_images(profile):
    assert profile.images.profile_picture_url.endswith("profile-photo.jpg")
    assert profile.images.background_image_url.endswith("cover-photo.jpg")


def test_about_is_the_full_text_not_the_section_heading(profile):
    assert profile.about.startswith("Mathematician and engineer.")
    assert "About" not in profile.about.split(".")[0]


def test_grouped_roles_are_flattened_onto_their_company(profile):
    titles = [e.title for e in profile.experience]
    assert titles == ["Principal Engineer", "Senior Software Engineer", "Software Engineer"]

    principal = profile.experience[0]
    assert principal.company == "Acme Corp"
    assert principal.company_url == "https://www.linkedin.com/company/acme/"
    assert principal.start_date == "Jan 2022"
    assert principal.end_date == "Present"
    assert principal.duration == "2 yrs 6 mos"
    assert principal.is_current is True
    assert principal.location == "Bengaluru, Karnataka, India"
    # inherited from the company header, which prints it only once
    assert principal.employment_type == "Full-time"
    assert "event-driven ingestion" in principal.description

    senior = profile.experience[1]
    assert senior.company == "Acme Corp"
    assert senior.employment_type == "Full-time"
    assert senior.location == "Bengaluru, Karnataka, India"
    assert senior.is_current is False
    assert senior.end_date == "Dec 2021"


def test_single_role_splits_company_from_employment_type(profile):
    role = profile.experience[2]
    assert role.company == "Globex"
    assert role.employment_type == "Internship"
    assert role.start_date == "Mar 2019"
    assert role.end_date == "May 2020"
    assert role.location == "Remote"


def test_current_company_is_derived_from_the_current_role(profile):
    assert profile.current_company == "Acme Corp"


def test_education(profile):
    assert len(profile.education) == 1
    school = profile.education[0]
    assert school.school == "University of Cambridge"
    assert school.degree == "Bachelor of Science - BS"
    assert school.field_of_study == "Mathematics"
    assert school.start_date == "2014"
    assert school.end_date == "2018"


def test_skills_carry_endorsement_counts(profile):
    assert [s.name for s in profile.skills] == ["Python", "Distributed Systems"]
    assert profile.skills[0].endorsement_count == 42
    assert profile.skills[1].endorsement_count is None


def test_certifications(profile):
    assert len(profile.certifications) == 1
    cert = profile.certifications[0]
    assert cert.name == "AWS Certified Solutions Architect"
    assert cert.issuer == "Amazon Web Services (AWS)"
    assert cert.issue_date == "Mar 2021"
    assert cert.expiration_date == "Mar 2024"
    assert cert.credential_id == "AWS-SAA-99120"
    assert cert.credential_url.startswith("https://aws.amazon.com/verification/")


def test_languages(profile):
    assert [(lang.name, lang.proficiency) for lang in profile.languages] == [
        ("English", "Native or bilingual proficiency"),
        ("Hindi", "Professional working proficiency"),
    ]


def test_missing_sections_yield_empty_lists_not_errors():
    minimal = "<html><body><main><h1>Nobody Here</h1></main></body></html>"
    profile = parser.parse_profile(
        minimal, public_id="nobody", profile_url="u", input_url="u"
    )
    assert profile.full_name == "Nobody Here"
    assert profile.experience == []
    assert profile.skills == []
    assert profile.about is None


def test_a_page_without_a_heading_is_not_a_profile():
    with pytest.raises(ValueError):
        parser.parse_profile(
            "<html><body><main><p>Sign in</p></main></body></html>",
            public_id="x",
            profile_url="u",
            input_url="u",
        )


def test_date_line_splitting():
    assert parser._split_date_line("Jan 2020 - Present · 4 yrs 2 mos") == (
        "Jan 2020",
        "Present",
        "4 yrs 2 mos",
    )
    assert parser._split_date_line("2016 - 2020") == ("2016", "2020", None)
    assert parser._split_date_line("4 yrs") == (None, None, "4 yrs")
