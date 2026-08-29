# LinkedIn Profile API

Give it a LinkedIn profile URL, get back structured JSON.

**Live:** <https://tross-h4pk.onrender.com> · [**Interactive docs**](https://tross-h4pk.onrender.com/docs) · [**Health**](https://tross-h4pk.onrender.com/api/v1/health)

```bash
curl --get "https://tross-h4pk.onrender.com/api/v1/profile" \
  --data-urlencode "url=https://www.linkedin.com/in/priyanshu-saxena07/"
```

`--data-urlencode` matters: a raw `https://` inside a query string trips some
proxies. Any client that encodes query parameters — `requests`, `axios`, `fetch`
with `URLSearchParams` — does this for you.

Reads LinkedIn's own Dash API — four GETs, no browser, ~1s. Every field the
brief asks for:

| | | | |
|---|---|---|---|
| name | headline | location | about |
| experience | education | skills | certifications |
| languages | profile image | background image | |

📖 **[How this was built](APPROACH.md)** — the design, the times it broke, and
what each failure taught. Start there for the reasoning rather than the
reference.

---

## Quick start

Python 3.11+.

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Mint a LinkedIn session — a browser opens, you sign in, the cookie is saved
where the service reads it. Once every few months, nothing to copy or paste:

```bash
playwright install chromium     # one-time, for the login helper
python scripts/login.py
```

Run it:

```bash
python -m uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/docs> and try it. The deployed instance is at
<https://tross-h4pk.onrender.com/docs>.

> **No credentials to hand?** Set `PROVIDER=public` and skip the login step
> entirely — anonymous, no account, no expiry. Costs you skills and
> certifications. See [Providers](#providers).

<details>
<summary>Windows notes — Application Control, and the browser provider</summary>

**`uvicorn.exe` blocked by Application Control.** Run it as a module instead;
`python.exe` is signed and allowed, the pip-generated shim isn't:

```bash
python -m uvicorn app.main:app --reload
```

Same for any `Scripts\` entry point: `python -m pytest`, `python -m ruff`.

**`PROVIDER=linkedin_scraper` + `--reload` can't coexist.** Playwright launches a
subprocess, and on Windows only `ProactorEventLoop` supports that — but uvicorn
forces `SelectorEventLoop` whenever it spawns workers (`reload or workers > 1`).
Drop `--reload` for that provider. Forcing Proactor back while keeping `--reload`
fails differently (`WinError 87`): the reloader passes an inherited socket that
can't be registered with IOCP. The app detects the wrong loop and returns
`500 incompatible_event_loop` with the fix in the message.

**Silent double-bind.** uvicorn sets `SO_REUSEADDR`, and Windows lets a second
process bind an already-bound port instead of refusing — leaving both alive with
the *older* one answering. If health disagrees with what you think you started:
`Get-NetTCPConnection -LocalPort 8000 -State Listen`.

</details>

---

## API

All endpoints under `/api/v1`. Interactive docs at `/docs`, schema at
`/openapi.json`.

### Authentication

**None by default.** `API_KEYS` ships empty, so every endpoint is open — which
is why the examples here run as-is.

To require a key, set `API_KEYS` (comma-separated for several); callers then
send `X-API-Key: <key>`, and requests without one get `401 missing_api_key`.
The service logs a warning at startup while it is open. Rate limiting applies
either way — keyed by IP when there is no key.

### `GET /profile` · `POST /profile`

| Parameter | Type | Description |
|---|---|---|
| `url` | string, required | Profile URL, or a bare public identifier |
| `refresh` | boolean | Bypass the 24h cache |

`url` is forgiving — `https://www.linkedin.com/in/x/`, `linkedin.com/in/x`,
`in.linkedin.com/in/x`, tracking query strings, `/details/experience/` suffixes,
and a bare `x` all resolve to the same profile. Company and school URLs are
rejected with a `400` that says so.

```bash
curl -X POST "https://tross-h4pk.onrender.com/api/v1/profile" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://www.linkedin.com/in/priyanshu-saxena07/"}'
```

The POST form needs no query encoding, which makes it the easier one to script.

**Response** — abridged. The full, unedited production response is committed
at [`docs/sample-response.json`](docs/sample-response.json):

```jsonc
{
  "success": true,
  "data": {
    "public_id": "priyanshu-saxena07",
    "full_name": "Priyanshu Saxena",
    "headline": "Full Stack Developer | Software Trainee @ Meril | MERN Sta...",
    "location": "India",
    "about": "I’m a backend developer in the making, currently wor...",
    "current_company": "Meril",
    "images": { "profile_picture_url": "https://media.licdn.com/...", 
                "background_image_url": "https://media.licdn.com/..." },
    "experience": [{                      // 8 entries
      "title": "Frontend Intern", "company": "Safe Your Web",
      "company_url": "https://www.linkedin.com/company/safeyourwebofficial/",
      "location": null,
      "start_date": "Oct 2024", "end_date": "Jan 2025",
      "is_current": false, "description": "..."
    }],
    "education": [{                       // 2 entries
      "school": "Parul University",
      "degree": "Bachelor of Technology - BTech",
      "field_of_study": "Artificial Intelligence"
    }],
    "skills":         [{ "name": "Nodebb" }],          // 27 entries
    "certifications": [{ "name": "Speaking Confidently and Effective" }],   // 4 entries
    "languages":      [],
    "source": "voyager",
    "scraped_at": "2026-08-29T08:57:47.004153Z"
  },
  "meta": { "provider": "voyager", "cached": false, "duration_ms": 1204 }
}
```

**Every field is optional.** LinkedIn shows different data to different viewers,
so an API that 404s on a missing `about` would be lying about what it knows.
Missing scalars are `null`, missing sections `[]`. Dates are LinkedIn's display
strings (`"Jan 2022"`), not ISO — the source has no day-of-month, and inventing
one would be inventing precision.

**Headers:** `X-Request-ID` (correlates with logs), `X-Cache` (`HIT`/`MISS`),
`X-RateLimit-Remaining`, `Retry-After` on 429.

### `GET /health`

Unauthenticated, always `200` — the payload carries the verdict, so health checks
don't flap during credential rotation.

```json
{ "status": "ok", "provider": { "name": "voyager", "configured": true,
  "authenticated": true, "detail": "session source: stored cookie" } }
```

### Errors

```json
{ "success": false, "error": { "code": "profile_not_found",
  "message": "...", "details": null }, "request_id": "c4f024ef7968" }
```

| Status | Code | Cause |
|---|---|---|
| 400 | `invalid_profile_url` | Not a member profile URL |
| 403 | `profile_not_accessible` | Exists, not visible to this session |
| 404 | `profile_not_found` | No such profile |
| 429 | `rate_limited` | Per-key limit; see `Retry-After` |
| 502 | `upstream_auth_error` | Session expired, revoked, or IP flagged |
| 502 | `upstream_endpoint_gone` | LinkedIn retired the endpoint (410) |
| 502 | `scrape_failed` | Payload shape changed |
| 503 | `provider_not_configured` | No session on this deployment |
| 504 | `upstream_timeout` | LinkedIn didn't respond |

Those last four are deliberately distinct: "session revoked", "endpoint
retired", "shape changed" and "LinkedIn is slow" have four different fixes.

---

## Providers

Same API, same schema, five transports. `PROVIDER` picks one; `data.source` in
each response says which answered.

| `PROVIDER` | Credentials | Data | Notes |
|---|---|---|---|
| **`voyager`** *(default)* | session | **all ten fields** | LinkedIn's Dash API. 4 GETs, ~1s, no browser |
| `public` | **none** | no skills/certs; redaction-prone | Anonymous schema.org record. Nothing to expire or ban |
| `embedded` | session | top card only | Reads JSON inlined in the page — thin since LinkedIn's 2026 UI rewrite |
| `linkedin_scraper` | session | — | Playwright. Parser targets the pre-2026 UI |
| `proxycurl` | vendor key | all fields | Licensed third party. Paid, ToS-clean |

**`public` caveat:** LinkedIn redacts fields for logged-out visitors
*inconsistently* — the same profile returned `"Co-chair"` then
`"Creator, Top Voice"` as its headline seconds apart. Redactions are filtered to
`null`, never emitted as asterisks, but responses are non-deterministic.

---

## Configuration

**`.env`** — secrets and per-deployment settings. Never committed; see
[`.env.example`](.env.example).

| Variable | Default | Notes |
|---|---|---|
| `API_KEYS` | *(empty)* | Empty ⇒ **open API**. Set to require `X-API-Key` |
| `PROVIDER` | `voyager` | See table above |
| `LINKEDIN_LI_AT` | — | Session cookie for remote deploys; local uses the cookie store |
| `COOKIE_STORE_PATH` | `.session/cookies.json` | Where `scripts/login.py` writes. Live credential |
| `PROXY_SERVER` / `_USERNAME` / `_PASSWORD` | — | Outbound proxy |
| `PROXYCURL_API_KEY` | — | For `PROVIDER=proxycurl` |
| `MAX_CONCURRENT_SCRAPES` | `1` | Raising this raises ban risk |
| `SCRAPE_MIN_INTERVAL_SECONDS` | `6` | Enforced floor between LinkedIn hits |
| `CACHE_TTL_SECONDS` | `86400` | Profile cache lifetime |
| `RATE_LIMIT_REQUESTS` / `_WINDOW_SECONDS` | `30` / `60` | Per-key |

**`config.json`** — committed, holds no secrets. Everything that changes when
*LinkedIn* changes: endpoint paths, the decoration id, entity `$type` fragments,
login selectors, auth-wall markers, browser tuning. Adapting to LinkedIn's next
change is usually a JSON edit rather than a release —
[and it has been](APPROACH.md). `CONFIG_FILE` overrides the path.

---

## Deployment

[`render.yaml`](render.yaml) is a Render blueprint: push to GitHub → **New →
Blueprint** → deploy. Render prompts for the secrets marked `sync: false`,
terminates TLS, and health-checks `/api/v1/health`.

Set `LINKEDIN_LI_AT` in the dashboard (printed by `scripts/login.py`), or use
`PROVIDER=public` and set nothing at all. The image is `python:3.12-slim` and no
provider except `linkedin_scraper` launches a browser, so the free tier is
enough.

Two free-tier caveats. There is **no persistent disk** — Render's free plan has
none — so the session is read from `LINKEDIN_LI_AT` on every boot rather than
recovered from cache. And instances **spin down when idle**: a request to a
sleeping service returns Render's own plain-text `404` with
`x-render-routing: no-server`, which is not your app answering. Poll
`/api/v1/health` until it returns JSON.

Any Docker host works: same env vars, bind `$PORT`. One worker by design — the
session is process-local state.

---

## Known limitations

**Legal.** Authenticated access is prohibited by the
[LinkedIn User Agreement §8.2](https://www.linkedin.com/legal/user-agreement).
Accounts used this way get restricted — **use a throwaway, never a personal
one.** `PROVIDER=public` avoids this entirely and stands on firmer ground
(`hiQ v. LinkedIn` concerned exactly that: public data, no login).

**Data.** Visibility is viewer-dependent: `null` means "not visible to *this*
session", not "not on the profile". `duration` ("4 yrs 2 mos") is always `null` —
LinkedIn computes it client-side. Image URLs are signed and expire. Only the
brief's ten fields are extracted.

**Reliability.** Voyager is a private API with no stability contract — LinkedIn
retired its predecessor mid-build (HTTP 410) and shipped an entirely new profile
UI during this project. Endpoint paths and type fragments live in `config.json`
so recovery is a config edit. Automated sign-in is impossible (the login page is
JavaScript-only), so sessions are minted by `scripts/login.py`. HTTP 999 means
the IP is flagged; a residential proxy is the usual remedy.

**Architecture.** Cache and rate limiter are in-process — correct on one
instance, wrong across replicas. Throughput is ~one profile per
`SCRAPE_MIN_INTERVAL_SECONDS`, by design; this is not a bulk-enrichment service.

---

## Development

```bash
python -m pytest      # 133 tests — no browser, no network, no LinkedIn account
python -m ruff check app tests scripts
```

Every mapper is a pure function over a committed fixture, and HTTP clients run
through `httpx.MockTransport`, so the suite is fully offline. That's the payoff
of the provider seam — five transports came and went without the routes, models,
cache or their tests changing.

`python scripts/probe.py <url> --save page.html` diagnoses a single fetch
end-to-end: session source, HTTP status, entity types found, and what the mapper
made of them.

**Secrets hygiene.** `.gitignore` excludes `.env`, `.session/`, and the cookie
store. Credentials reach the service only through environment variables or the
local cookie store — nothing secret is in this repository, and `config.json` is
committed precisely because it contains none. If a cookie leaks: change the
account's password, which invalidates it immediately.
