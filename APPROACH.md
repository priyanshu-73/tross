# How this was built

A LinkedIn profile API that returns all ten required fields in ~1 second, over
plain HTTP, with no browser at runtime.

This document explains **how it works**, **how we found it**, and **what it cost
to get there** — including four dead ends that shaped the final design.

---

## 1. What it does

```
GET /api/v1/profile?url=https://www.linkedin.com/in/williamhgates/
```

```
┌─────────────┐   normalise    ┌──────────────┐   4 GETs    ┌──────────────┐
│  URL in     │ ─────────────► │  Provider    │ ──────────► │  LinkedIn    │
│  any shape  │   public_id    │  (voyager)   │ ◄────────── │  Dash API    │
└─────────────┘                └──────────────┘  entities   └──────────────┘
                                      │
                                      ▼  filter by $type
                               ┌──────────────┐
                               │   mapper     │  pure function
                               └──────────────┘
                                      │
                                      ▼
                            LinkedInProfile (JSON)
```

Four requests per profile:

| Endpoint | Returns |
|---|---|
| `identity/dash/profiles` | profile, positions, educations, companies, schools, geo |
| `identity/dash/profileSkills` | full skill list |
| `identity/dash/profileCertifications` | certifications |
| `identity/dash/profileLanguages` | languages |

The last three are best-effort — a member with no certifications isn't a failed
request. The first is required.

---

## 2. How the LinkedIn API actually works

### The endpoint

```http
GET /voyager/api/identity/dash/profiles
      ?q=memberIdentity
      &memberIdentity=williamhgates
      &decorationId=com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-63

Cookie:      li_at=…; JSESSIONID=…; bcookie=…; lidc=…   (the whole jar)
csrf-token:  ajax:1234567890                            (= JSESSIONID, unquoted)
accept:      application/vnd.linkedin.normalized+json+2.1
x-restli-protocol-version: 2.0.0
```

Three details that each cost a debugging session:

**The `decorationId` is the whole game.** Without it the response contains one
entity — the bare profile. With `FullProfileWithEntities-63` it contains **46**:
every position, education, company and school. Decorations are LinkedIn's
server-side projection system; the `-63` is a version that will eventually move.

**`csrf-token` is just `JSESSIONID` with the quotes stripped.** LinkedIn issues
it on any page request, so the client bootstraps it rather than asking an
operator for it.

**The whole cookie jar is required.** `li_at` alone gets a 302 loop back to the
same URL — LinkedIn refusing to serve a session missing the cookies it issued
alongside. This presents as `TooManyRedirects`, which looks nothing like an auth
problem.

### The response shape

Dash responses are **normalised**: a flat `included[]` array where every entity
carries a `$type`, plus a `data` envelope of references between them.

```jsonc
{
  "data": { "*elements": ["urn:li:fsd_profile:ADA"] },
  "included": [
    { "$type": "…identity.profile.Profile",  "firstName": "Ada", "headline": "…" },
    { "$type": "…identity.profile.Position", "title": "Principal Engineer",
      "companyUrn": "urn:li:fsd_company:1",
      "dateRange": { "start": { "month": 1, "year": 2022 } } },
    { "$type": "…organization.Company", "entityUrn": "urn:li:fsd_company:1",
      "name": "Acme Corp", "url": "https://www.linkedin.com/company/acme/" }
  ]
}
```

That shape is a gift. The mapper **filters by type** and never walks a nested
tree LinkedIn is free to restructure:

```python
positions = of_type(entities, "identity.profile.Position")
companies = {e["entityUrn"]: e for e in of_type(entities, "organization.Company")}
```

Cross-references resolve by urn — that's how a position gets a real company URL
rather than an unusable internal id.

### Type matching is anchored, deliberately

```python
e["$type"].endswith(fragment)      # not: fragment in e["$type"]
```

Suffix matching survives LinkedIn versioning the package path
(`com.linkedin.voyager.dash.identity.profile.*`), which it does often, while
still pinning the leaf class. **Anchoring matters**: `PositionGroup` *contains*
`identity.profile.Position`, so an unanchored match returns every role twice.
It did — 16 positions where there were 8 — and only live data revealed it.

---

## 3. How we found that endpoint

The obvious endpoint — `identity/profiles/{id}/profileView`, the one every
tutorial and library still references — returns **HTTP 410 Gone**. LinkedIn
retired it, with no notice and no announced replacement, because a private API
owes you neither.

Guessing replacements is a losing game. Instead:

**Step 1 — the page's client must fetch this data from somewhere.** Endpoint ids
are compiled into LinkedIn's JavaScript bundles, which are public CDN files. No
credentials needed to read them.

**Step 2 — mine them.** 14 bundles, 3.9 MB, scanned for URL-shaped strings:

```
/voyager/api/voyagerSocialDashNormComments
/voyager/api/voyagerSearchDashSearchHome
/voyager/api/voyagerContentcreationDashGuiderPrompts
```

None are profile endpoints, but the **naming convention** is right there:
`/voyager/api/voyager<Domain>Dash<Entity>`, alongside a REST equivalent
`identity/dash/<collection>`.

**Step 3 — test candidates against a live session**, with `/voyager/api/me` as a
control to prove the session itself worked:

| Endpoint | Result |
|---|---|
| `/voyager/api/me` | 200 — session is valid |
| `identity/profiles/{id}/profileView` | **410** — retired |
| `identity/dash/profiles?q=memberIdentity` | **200** ✅ |
| `voyagerIdentityDashProfileCards` | 400 |
| `identity/dash/profileCards` | 404 |
| `/voyager/api/graphql` | 403 |

**Step 4 — find the decoration.** The working endpoint returned one entity.
Trying known decoration ids turned one into 46.

The method generalises: *read what the client reads, don't guess what the server
accepts.* When this endpoint is retired too — and it will be — that's the
procedure, and the paths are in `config.json` so the fix is an edit, not a
release.

---

## 4. The four dead ends

Each was abandoned for a specific, measured reason. Each left something behind.

### Browser rendering (Playwright)

Drive Chromium, sign in, parse the DOM. It worked. It also cost **5–15s** per
profile (sections truncate at three entries; the rest is behind five more page
loads) and **~400 MB** of memory, which sets the deployment floor.

*What it left behind:* the instinct to parse by **structure, not appearance**.
LinkedIn rotates CSS classes, so the parser targeted section anchor ids
(`div#experience`) its own navigation depends on, and the `aria-hidden` text
pairs its accessibility layer requires. That "find the surface they can't afford
to change" question became the through-line of the whole project.

### The old Voyager endpoint

`profileView` → 410, described above.

*What it left behind:* the realisation that **fixtures cannot falsify an
assumption they encode**. That provider had clean code, typed errors and full
test coverage — against an interface that no longer existed. Everything
LinkedIn-shaped moved into `config.json` the same day.

### Reading JSON out of the page

If endpoints are unstable, stop naming them: LinkedIn used to server-render
profiles with the payloads inlined (`<code id="bpr-guid-N">` blocks). Fetch
`/in/<id>/`, read the JSON from the HTML. The profile URL is the one address
LinkedIn cannot retire.

Then LinkedIn shipped a **new profile UI** mid-build — a React Server Components
app. Measured on the response:

| Marker | Count |
|---|---|
| `<code id="bpr-guid-…">` blocks | 0 |
| `"$type"` / `"included"` | 0 |
| `application/ld+json` | 0 |
| `span[aria-hidden="true"]` | 0 |
| Visible text in 1.1 MB of HTML | 2,540 chars |

That invalidated *two* providers at once — the inlined-JSON extractor and the
DOM parser both describe a UI that no longer exists. Decoding the 897 KB
hydration payload showed only the top card; everything else loads client-side.

*What it left behind:* the `public` provider (below), and a healthy respect for
how fast this ground moves.

### Automated sign-in

The plan was to stop pasting cookies and let the service log in. Built it —
fetch the page, harvest the CSRF token, post credentials — then checked the
assumption before shipping:

| | Old login page | Current |
|---|---|---|
| Size | 52 KB | 488 KB |
| `<form>` elements | 3 | **0** |
| Password input | named | **no `name`** |

Two independent parsers agreed. LinkedIn renders sign-in entirely in React;
there is nothing for an HTTP client to submit. **Automated login is not
possible**, so it moved out of the service into `scripts/login.py` — a real
browser, run once every few months, writing the cookie jar where the service
reads it.

*What it left behind:* its own tests caught that `/checkpoint/lg` was in the
challenge-detection list, and LinkedIn's *successful* login endpoint is
`/checkpoint/lg/login-submit` — so every successful sign-in would have been
reported as a security checkpoint.

---

## 5. The session problem

Sessions kept dying. Not from expiry — `li_at` is good for about a year — but
because **LinkedIn rotates a session used from an address it wasn't issued to**.
Copying a cookie from a laptop to a server is exactly that pattern.

The evidence was unambiguous once measured:

```
set-cookie: li_at=delete me
```

Delivered on a 302 back to the same URL, which is why it *presented* as a
redirect loop rather than an auth error. That signal is now detected and reported
as "session revoked — mint a new one."

Three fixes came out of it:

1. **Replay the whole jar**, not just `li_at`.
2. **Clear the jar before re-authenticating** — a revoked cookie travels with the
   login request and poisons its own recovery.
3. **Mint the cookie on the machine that will use it**, which is what
   `scripts/login.py` does.

---

## 6. No credentials at all

Asked whether any of this could work without a session, the honest first answer
was no: an anonymous profile request returns **HTTP 999**, LinkedIn's "you look
automated" reply.

That was wrong. A real visitor never arrives cold — they land on the homepage
first and collect guest cookies. Do the same, carry them forward, and the
identical URL returns **200** with a full schema.org `Person` record: name,
headline, location, about, photo, employers with roles and dates, schools,
languages, follower count.

That became `PROVIDER=public`: no account, no cookie, nothing to expire, nothing
to ban, and the strongest legal footing here — `hiQ v. LinkedIn` concerned
exactly this, public data accessed without logging in.

Its limits are real and documented: no skills or certifications at any
visibility, and LinkedIn redacts fields for guests **non-deterministically**. The
same profile, seconds apart:

```
"headline": "Co-chair"              # jobTitle came through
"headline": "Creator, Top Voice"    # redacted; fell back to the subtitle
```

Redactions arrive as `"************ ******"` and are filtered to `null`. An API
emitting `"******"` as a company name would be worse than one admitting it
doesn't know.

---

## 7. Architecture

**A provider interface, not a scraper with routes bolted on.** The HTTP layer
never imports Playwright or an HTTP client. It knows one method:

```python
async def fetch_profile(*, public_id, profile_url, input_url) -> LinkedInProfile
```

That seam is why **five transports** passed through this project without the
routes, models, cache, rate limiter or their tests changing once — and why the
entire API surface is testable against a stub with no browser and no account.

```
app/
├── api/routes.py            endpoints — no provider knowledge
├── models.py                the contract
├── providers/
│   ├── base.py              the seam
│   ├── session.py           cookie jar, revocation detection
│   ├── voyager/             DEFAULT — Dash API (client + pure mapper)
│   ├── public/              anonymous — JSON-LD (pure mapper)
│   ├── embedded/            page-inlined JSON
│   ├── linkedin_scraper/    Playwright
│   └── proxycurl.py         licensed vendor
├── services/                cache, rate limiter
└── utils/                   URL normalisation, redaction filter
```

**Everything volatile lives in `config.json`** — endpoint paths, the decoration
id, entity type fragments, login selectors, auth-wall markers. A direct
consequence of the 410: any LinkedIn-shaped constant in Python is a liability.

**Absence is data.** Every field is optional; redactions become `null`.

**Failures are typed.** "No such profile", "not visible to this session",
"session revoked", "endpoint retired", "IP flagged" and "shape changed" are six
problems with six fixes, so they get six status codes and six messages.

**Providers may not lie about what they are.** With no session LinkedIn still
returns *a* page — the logged-out one. The `embedded` provider parsed it happily
and labelled the result `source: embedded`, handing back a thin profile that
looked rich. It now refuses. A silently degraded response is worse than an error.

**Speed isn't the constraint; the account is.** Voyager is ~1s where the browser
was ~15s, which makes it *easier* to burn an account's rate budget. The throttle,
the concurrency ceiling of one, and the 24-hour cache all carried over unchanged.

---

## 8. Two bugs only live data could find

Both shipped green through a full fixture suite.

**Experience returned 16 entries where there were 8.** `PositionGroup` contains
`identity.profile.Position`, so substring matching double-counted every role.

**Certifications returned 8 where there were 4.** The section endpoints re-send
entities the profile call already returned; merging them naively duplicated
everything.

Neither was reachable from a fixture, because the fixture encoded my
understanding — which was the thing that was wrong. Both now have regression
tests named after the failure, and the shared fixture deliberately includes a
`PositionGroup` and a second `Profile` (the viewer's own) so the traps stay
covered.

---

## 9. Why Python, when I mostly write TypeScript

I'm primarily a TypeScript and Node developer; instinct said Fastify, Playwright
Node bindings and Zod. Three reasons I chose otherwise:

**The OpenAPI docs are a deliverable.** FastAPI derives a complete interactive
spec from the Pydantic models and route signatures that had to exist anyway.
`/docs` isn't maintained alongside the code — it *is* the code. In Node I'd be
hand-writing a spec that drifts the first time I'm in a hurry.

**Parsing is Python's home turf.** BeautifulSoup with `lxml`, soupsieve's
`:scope`, and Playwright's Python API are a mature, well-trodden combination. The
parsing layer was the highest-risk part of this project and deserved the boring
option, not the one I'm more fluent in.

**Pydantic makes "everything is optional" enforceable.** One definition drives
schema, validation, serialisation and published types, so no field can quietly
become required in the response but not the docs.

The language was a *tool choice*, not an identity. The decisions that determine
whether this is any good — the provider seam, depending on stable surfaces,
treating absence as data, typed errors — are language-independent. I'd have made
the same calls in TypeScript.

---

## 10. On building this with AI

Built with heavy AI assistance, and I'd rather say so than have someone wonder.

The interesting claim isn't "AI writes code." It's that **AI collapses the cost
of working outside your comfort zone** — which is why picking Python over my
usual stack stopped being a real cost. Ten years ago, "I mostly write Node but
this problem wants Python" ended with a Node solution.

What it doesn't supply is judgement about *what to depend on*. Generated code has
a specific failure signature: it's **plausible**. It compiles, the tests pass,
and it's wrong where it matters:

| What happened | Why tests didn't catch it |
|---|---|
| Parser built on CSS selectors | It's what most LinkedIn scraping code does, so it's what gets suggested |
| URLs hardcoded across three modules | Reasonable-looking; the values needing urgent edits were buried deepest |
| API key scheme missing from OpenAPI | Auth worked — only `/docs` was broken |
| Grouped roles silently dropped employment type | Response was well-formed and incomplete |
| 500-handler test passing for the wrong reason | `TestClient` re-raises by default; it tested the harness |
| A whole provider on a retired endpoint | Fixtures encoded the same stale assumption |
| `PositionGroup` doubling every role | Only a live row count revealed it |

The last two are the ones worth dwelling on. Neither was a coding error. One was
wrong about *which interface to depend on*; the other about *what a string match
means at the edges*. Both were invisible to a green suite and obvious the instant
real data appeared.

And the fix for the biggest one didn't come from code review. After measuring
that the new UI shipped no usable data over HTTP, I concluded there was no
solution and said so. The reply was *"are you sure? is there really no
solution?"* — which sent me to mine LinkedIn's JS bundles and find the endpoint
this entire service now runs on.

That is the part a person still has to bring: not checking the output, but
refusing a premature conclusion.

---

## 11. What I'd do differently at scale

- **Redis** for cache and rate limiter — both in-process today, correct on one
  instance and wrong across replicas.
- **A queue with per-account workers** instead of fetching inside the request, so
  throughput scales with accounts rather than patience.
- **A pool of sessions and residential proxies** with health scoring; IP
  reputation matters more than any code in this repo.
- **Scheduled contract tests against real saved payloads**, so LinkedIn's next
  change appears in CI rather than in someone's error budget. Given that LinkedIn
  retired an endpoint *and* shipped a new profile UI during this build, that's
  not hypothetical — it's the highest-value item on this list.
