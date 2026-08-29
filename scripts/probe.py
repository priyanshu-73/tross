"""Diagnose one profile fetch, end to end.  `python scripts/probe.py <url>`

Answers the questions the API's error codes can only summarise:

* Did LinkedIn accept the session, or quietly serve a sign-in wall?
* Did the page carry embedded JSON, and how much?
* Which entity ``$type`` strings actually came back — the thing most likely to
  drift from what `mapper.py` expects?
* What did the mapper make of it?

Add ``--save out.html`` to keep the raw page. That file is the fixture you want
when adjusting the mapper: reconciling against a real payload offline beats
hitting LinkedIn repeatedly, and it keeps the account's request budget intact.

Nothing here is printed that identifies your session — the cookie is used, never
shown.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.errors import AppError  # noqa: E402
from app.providers.embedded import extractor, mapper  # noqa: E402
from app.providers.session import LinkedInAuthenticator  # noqa: E402
from app.site_config import get_site_config  # noqa: E402
from app.utils.url import normalize_profile_url  # noqa: E402


async def probe(raw_url: str, save_to: str | None) -> int:
    settings = get_settings()
    site = get_site_config()
    config = site.embedded

    auth = LinkedInAuthenticator(settings, site.auth, site.browser.user_agent)
    if not auth.configured:
        print("No session source. Set LINKEDIN_EMAIL + LINKEDIN_PASSWORD or LINKEDIN_LI_AT.")
        return 2

    try:
        public_id, profile_url = normalize_profile_url(raw_url)
    except AppError as exc:
        print(f"Not a usable profile URL: {exc.message}")
        return 2

    print(f"public_id   : {public_id}")
    print(f"requesting  : {config.profile_url(public_id)}\n")

    async with httpx.AsyncClient(
        timeout=config.timeout_seconds,
        follow_redirects=True,
        headers={"user-agent": site.browser.user_agent, **config.headers},
    ) as client:
        try:
            await auth.apply(client)
        except AppError as exc:
            print(f"could not establish a session: {exc.message}")
            return 2
        print(f"session     : {auth.source}")
        response = await client.get(config.profile_url(public_id))

    html = response.text
    print(f"HTTP status : {response.status_code}")
    print(f"final URL   : {response.url}")
    print(f"body length : {len(html):,} bytes")
    if response.history:
        print(f"redirects   : {' -> '.join(str(r.url) for r in response.history)}")

    lowered = html.lower()
    hits = [m for m in config.authwall_markers if m in lowered]
    print(f"authwall    : {'YES - ' + ', '.join(hits) if hits else 'no'}")

    if save_to:
        Path(save_to).write_text(html, encoding="utf-8")
        print(f"saved       : {save_to}")

    # --- what is actually embedded in the page ---
    blobs = extractor.iter_json_blobs(html)
    entities = extractor.collect_included(blobs)
    print(f"\nJSON blobs  : {len(blobs)}")
    print(f"entities    : {len(entities)}")

    if entities:
        counts = collections.Counter(e.get("$type", "?") for e in entities)
        print("\nentity types found (this is what mapper.py must match):")
        for type_name, count in counts.most_common(25):
            expected = [
                key for key, frag in config.entity_types.items() if frag and frag in type_name
            ]
            marker = f"  <- maps to '{expected[0]}'" if expected else ""
            print(f"  {count:4d}  {type_name}{marker}")

        unmatched = set(config.entity_types) - {
            key
            for type_name in counts
            for key, frag in config.entity_types.items()
            if frag and frag in type_name
        }
        if unmatched:
            print(f"\n  NOT FOUND in page: {', '.join(sorted(unmatched))}")
            print("  -> adjust config.json 'embedded.entity_types' to match the names above")

    person = extractor.find_json_ld_person(blobs)
    print(f"\nJSON-LD     : {'found (' + str(person.get('name')) + ')' if person else 'absent'}")

    # --- what the mapper makes of it ---
    print("\n--- mapped result ---")
    try:
        profile = mapper.map_profile(
            entities,
            blobs,
            public_id=public_id,
            profile_url=profile_url,
            input_url=raw_url,
            config=config,
            vocab=site.vocabulary,
        )
    except ValueError as exc:
        print(f"mapping failed: {exc}")
        return 1

    print(f"name        : {profile.full_name}")
    print(f"headline    : {profile.headline}")
    print(f"location    : {profile.location}")
    print(f"about       : {(profile.about or '')[:70]}{'...' if profile.about else ''}")
    print(f"picture     : {'yes' if profile.images.profile_picture_url else 'no'}")
    print(f"experience  : {len(profile.experience)}")
    print(f"education   : {len(profile.education)}")
    print(f"skills      : {len(profile.skills)}")
    print(f"certs       : {len(profile.certifications)}")
    print(f"languages   : {len(profile.languages)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="LinkedIn profile URL or public identifier")
    parser.add_argument("--save", metavar="PATH", help="write the raw HTML here")
    args = parser.parse_args()
    return asyncio.run(probe(args.url, args.save))


if __name__ == "__main__":
    raise SystemExit(main())
