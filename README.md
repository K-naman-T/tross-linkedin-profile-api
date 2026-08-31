# LinkedIn Profile API

> Reverse-engineered LinkedIn profile scraper. **No browser automation** — it
> talks directly to LinkedIn's internal *Voyager* REST API (the same endpoint
> their own web frontend calls). Given a LinkedIn profile URL or slug, it
> returns structured JSON: name, headline, location, about, experience,
> education, skills, certifications, languages, profile images, and contact info.

Built for the **Tross Software Engineer** take-home challenge.

---

## Approach (the reverse-engineering part)

LinkedIn does not expose profile data through any official API for general use.
The path used here was **discovered from live network traffic**, not docs:

1. **Capture.** Log into LinkedIn in a normal browser, open a public profile, and
   watch DevTools → Network → XHR. The frontend issues a `GET` to
   `https://www.linkedin.com/voyager/api/identity/dash/profiles` with query
   `q=memberIdentity&memberIdentity={slug}&decorationId=...`.
2. **Auth recipe** (the bit naive scrapers get wrong → `403 CSRF check failed`):
   - `li_at` — login session cookie
   - `JSESSIONID` — CSRF session cookie, **same session**
   - `csrf-token` header **must equal `JSESSIONID` with surrounding quotes stripped**
   - headers: `x-restli-protocol-version: 2.0.0`,
     `accept: application/vnd.linkedin.normalized+json+2.1`, `x-li-lang: en_US`
3. **Response shape.** A flat `included[]` array. The primary profile has an
   `entityUrn` containing `fsd_profile:` and a `firstName`. Experience / Education
   / Skills / Certifications / Languages are sibling entries keyed by their
   `$type` suffix.
4. **Two endpoints.** `dash/profiles` (primary profile + most fields) and
   `profileContactInfo` (email / websites / twitter — gated, often empty).

See [`linkedin_scraper.py`](linkedin_scraper.py) for the full commented derivation.

**Design decisions:**
- A **thin, dependency-light client** is implemented directly rather than depending on
  the unmaintained `linkedin-api` PyPI package (last release 2019, authenticates via
  email+password, triggers 2FA CHALLENGE, sleeps 2–5s per request). A hand-written
  client exposes the Voyager mechanics directly.
- **Graceful failure** is bounded: 401/403 → "refresh cookies", 404 → not found,
  429 → rate limited, 5xx/timeout → upstream error. No raw tracebacks are exposed.
- **Resilience:** one reused `curl_cffi` Session, a bounded TTL cache (fewer upstream
  requests → lower ban risk), and a single retry on transient upstream errors.
- **Zero browser automation.** Every request is a hand-built `curl_cffi` call to the
  Voyager REST API — no Selenium, Playwright, or headless browser in the API path.
  `bookmarklet.html` / `test_bookmarklet_gd.py` are dev-only helpers and are excluded
  from the repository.

---

## API

Base URL: `https://<host>` (see Deploy).

| Method | Path | Params | Returns |
|---|---|---|---|
| GET | `/` | — | service info |
| GET | `/health` | — | `{status, auth_configured, epoch}` |
| GET | `/profile` | `url` *or* `slug` | `ProfileResponse` |
| POST | `/profile` | JSON `{url\|slug}` | `ProfileResponse` |

### Example
```bash
curl "https://<host>/profile?slug=satyanadella"
```
```json
{
  "status": "success",
  "source": "linkedin_voyager_api",
  "fetched_at": "2026-08-30T03:10:00Z",
  "data": {
    "basic": {"first_name": "Satya", "last_name": "Nadella", "full_name": "Satya Nadella",
              "headline": "Chairman and CEO at Microsoft", "location": "Redmond, WA",
              "industry": "Technology", "public_id": "satyanadella",
              "profile_url": "https://www.linkedin.com/in/satyanadella"},
    "about": "...",
    "experience": [{"title": "Chairman and CEO", "company": "Microsoft", "is_current": true, ...}],
    "education": [...], "skills": [...], "certifications": [...], "languages": [...],
    "profile_images": {"profile_picture": "https://...", "cover_photo": "https://..."},
    "contact_info": {"email": "", "websites": [...], "twitter": ""}
  }
}
```

Interactive docs: `GET /docs` (Swagger UI).

---

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env: paste li_at + JSESSIONID from a dedicated LinkedIn account
#   (DevTools -> Application -> Cookies -> www.linkedin.com)

uvicorn main:app --port 8099
```

### Production cookies (headless VPS)

On a headless server the local Firefox profile is unavailable. Ship the **entire**
`linkedin.com` cookie set (not just `li_at`) so the session is consistent:

- `cookies.json` — a `{name: value}` JSON dump of all `linkedin.com` cookies, or
- `LINKEDIN_COOKIES` — the same JSON in an environment variable (preferred for secret managers).

`LI_AT_COOKIE` alone remains functional but provides the weakest fidelity; the full jar
is recommended for production. Keep `cookies.json` out of version control (already gitignored). See
`cookies.json.example` for the expected shape.

Run tests:
```bash
pytest tests/test_client.py::test_parse_synthetic -q   # offline, no network
pytest tests/test_client.py::test_live_gate -q         # needs .env + network
```

---

## Deploy

Two options (both used the same FastAPI app):

> **Cookies on the server:** the application reads `cookies.json` / `LINKEDIN_COOKIES` at
> startup (see *Production cookies* above). The local Firefox fallback is unavailable on a headless host.

**A. Cloudflare quick tunnel (instant public HTTPS, no DNS):**
```bash
uvicorn main:app --port 8099 &
cloudflared tunnel --url http://localhost:8099
# -> prints a https://*.trycloudflare.com URL
```

**B. Permanent subdomain (nginx + certbot):**
```nginx
# /etc/nginx/sites-available/linkedin-api
server {
  listen 443 ssl;
  server_name li-api.yourdomain.com;
  location / { proxy_pass http://127.0.0.1:8099; }
}
```
```bash
certbot --nginx -d li-api.yourdomain.com
systemctl enable --now linkedin-api   # systemd unit runs uvicorn
```

---

## Security

- **Credentialed proxy.** The API proxies LinkedIn session cookies.
  Public deployments require `API_KEY` (environment variable); callers must send
  `Authorization: Bearer <key>` (or `?key=<key>`). Without this, the endpoint is
  an open relay. CORS is open (`*`) but credentials are never accepted cross-origin.
- **No PII persistence** — only an in-memory TTL cache. No disk storage is used.
- **Dedicated account and low volume.** Automated access violates LinkedIn ToS §8.2;
  a dedicated account and a low request rate are required. The full cookie jar and
  `curl_cffi` TLS impersonation reduce fingerprint anomalies.

## Known limitations (documented, not bugs)

- **Cookies expire in 3–7 days** → 401/403. No browser automation is used, so
  there is no auto-refresh; re-authenticate the dedicated account in a browser to refresh.
- **Account ban risk.** Automated access violates LinkedIn ToS §8.2. A
  dedicated account and low request volume are required.
- **Partial data.** Email / contact info is gated and frequently empty.
  Full skills lists may be truncated.
- **Decoration schema rotation.** The `decorationId` pin (`-93`) rotates every
  4–8 weeks; re-capture the live value if the API starts returning 400/empty.
- **Datacenter IP.** Running from a cloud VM raises LinkedIn's fraud score;
  acceptable for low-volume demo use, documented as a limitation.
- **Legal.** Scraping *public* LinkedIn data is lawful under US precedent
  (hiQ v. LinkedIn), but violates LinkedIn ToS; storing EU personal data implicates
  GDPR. Do not persist PII.
