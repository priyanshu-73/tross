import pytest

from app.errors import InvalidProfileURL
from app.utils.url import normalize_profile_url

CANONICAL = "https://www.linkedin.com/in/ada-lovelace-1a2b3c/"


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.linkedin.com/in/ada-lovelace-1a2b3c/",
        "https://www.linkedin.com/in/ada-lovelace-1a2b3c",
        "http://linkedin.com/in/ada-lovelace-1a2b3c",
        "www.linkedin.com/in/ada-lovelace-1a2b3c/",
        "linkedin.com/in/ada-lovelace-1a2b3c",
        "https://in.linkedin.com/in/ada-lovelace-1a2b3c",
        "https://www.linkedin.com/in/ada-lovelace-1a2b3c/?originalSubdomain=in&trk=abc",
        "https://www.linkedin.com/in/ada-lovelace-1a2b3c/details/experience/",
        "  https://www.linkedin.com/in/ada-lovelace-1a2b3c/  ",
        "ada-lovelace-1a2b3c",
    ],
)
def test_accepts_every_shape_users_paste(raw):
    public_id, canonical = normalize_profile_url(raw)
    assert public_id == "ada-lovelace-1a2b3c"
    assert canonical == CANONICAL


def test_legacy_pub_urls_are_supported():
    public_id, _ = normalize_profile_url("https://www.linkedin.com/pub/ada-lovelace/1/2/3")
    assert public_id == "ada-lovelace"


def test_percent_encoded_slugs_are_decoded():
    public_id, _ = normalize_profile_url("https://www.linkedin.com/in/%E5%BC%A0%E4%BC%9F-1a2b")
    assert public_id == "张伟-1a2b"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "https://example.com/in/ada-lovelace",
        "https://www.linkedin.com/",
        "https://www.linkedin.com/feed/",
        "https://twitter.com/ada",
    ],
)
def test_rejects_non_profile_input(raw):
    with pytest.raises(InvalidProfileURL):
        normalize_profile_url(raw)


def test_company_urls_get_a_targeted_message():
    with pytest.raises(InvalidProfileURL) as excinfo:
        normalize_profile_url("https://www.linkedin.com/company/acme/")
    assert "not member profiles" in str(excinfo.value)
