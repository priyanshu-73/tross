"""Mint a LinkedIn session, once, in a real browser.

    python scripts/login.py

It opens Chromium, waits for you to finish signing in (including any 2FA or
CAPTCHA), then saves the session to `COOKIE_STORE_PATH` — where the service
reads it automatically on its next start. Nothing to copy or paste locally.

**Why a browser is required.** LinkedIn's sign-in page is now rendered entirely
in JavaScript: it contains no HTML `<form>`, and its password input carries no
`name` attribute, so an HTTP client has nothing to harvest or submit. Signing in
is the one operation that genuinely needs a browser — which is why it lives here,
in a script you run every few months, rather than inside the service. Profile
fetching stays pure HTTP.

For a remote deployment the cookie is also printed, so you can set it there as
`LINKEDIN_LI_AT`. It is a live credential: treat it like a password, and never
commit it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from playwright.async_api import async_playwright  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.providers.session import CookieStore  # noqa: E402
from app.site_config import get_site_config  # noqa: E402


async def main() -> int:
    settings = get_settings()
    site = get_site_config()
    state_path = Path(settings.session_state_path)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False, args=site.browser.launch_args
        )
        context = await browser.new_context(
            user_agent=site.browser.user_agent,
            viewport=site.browser.viewport.model_dump(),
            locale=site.browser.locale,
        )
        page = await context.new_page()
        await page.goto(site.linkedin.login_url)

        print()
        print("A browser window is open.")
        print("Sign in to LinkedIn there, complete any verification, reach the feed.")
        print("Then come back here and press Enter.")
        print()
        await asyncio.get_running_loop().run_in_executor(None, input)

        cookies = await context.cookies()
        li_at = next((c for c in cookies if c["name"] == "li_at"), None)

        if li_at is None:
            print("No li_at cookie found - the sign-in did not complete.", file=sys.stderr)
            await browser.close()
            return 1

        state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(state_path))
        await browser.close()

    # Save the whole jar, not just li_at: LinkedIn redirect-loops a request that
    # carries the session cookie without the others it issued alongside it.
    jar = {
        c["name"]: c["value"]
        for c in cookies
        if "linkedin" in (c.get("domain") or "") and c.get("value")
    }
    CookieStore(settings.cookie_store_path).save_all(jar)

    print()
    print(f"Browser state saved to  {state_path}")
    print(f"Session ({len(jar)} cookies) saved to {settings.cookie_store_path}")
    print()
    print("The service picks this up on its next start - nothing to paste.")
    print()
    print("For a remote deployment, set this in its environment instead:")
    print()
    print(f"LINKEDIN_LI_AT={li_at['value']}")
    print()
    print("Keep it secret. It is equivalent to being signed in as that account.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
