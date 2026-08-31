"""
LinkedIn Voyager client — reverse-engineered, NO browser automation.

This module talks directly to LinkedIn's internal *Voyager* REST API: the same
endpoint their own web frontend calls to render a profile. No Selenium,
Playwright, or headless browser is used anywhere in this project.

--------------------------------------------------------------------------------
REVERSE-ENGINEERING NOTES (how this was discovered)
--------------------------------------------------------------------------------
1. Capture: log into LinkedIn in a normal browser, open a public profile, open
   DevTools -> Network -> filter XHR/fetch. The frontend issues a GET to
       https://www.linkedin.com/voyager/api/identity/dash/profiles
   with query string q=memberIdentity&memberIdentity={slug}&decorationId=...

2. Auth recipe (the part every naive scraper gets wrong -> 403 "CSRF check
   failed"):
     - li_at      : login session cookie (copy from DevTools -> Application -> Cookies)
     - JSESSIONID : CSRF session cookie, SAME session
     - csrf-token : MUST equal the JSESSIONID value with surrounding double
                    quotes STRIPPED. It is NOT a separate token.
     - headers    : x-restli-protocol-version: 2.0.0
                    accept: application/vnd.linkedin.normalized+json+2.1
                    x-li-lang: en_US

3. Response shape: a flat `included[]` array. The primary profile object has an
   `entityUrn` containing "fsd_profile:" and a `firstName`. Experience/Education/
   Skills/Certifications/Languages are sibling entries distinguished by their
   `$type` suffix.

4. Two endpoints are used:
     (a) dash/profiles  -> primary profile + experience/education/skills/certs/
                           languages/profile images/about
     (b) profileContactInfo -> email / websites / twitter (gated; may be empty)

5. Known instability: the `decorationId` schema pin (the "-93" suffix) rotates
   every 4-8 weeks. If the API starts returning 400/empty, re-capture the
   current value from live browser traffic.
--------------------------------------------------------------------------------
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from curl_cffi import requests as curl_requests


# ---------------------------------------------------------------------------
# Exceptions (bound the tail — never leak raw tracebacks to the caller)
# ---------------------------------------------------------------------------
class LinkedInError(Exception):
    """Base class for all client errors."""


class LinkedInAuthError(LinkedInError):
    """401/403 — cookies expired or invalid. Refresh li_at + JSESSIONID."""


class LinkedInNotFound(LinkedInError):
    """Profile does not exist or is private."""


class LinkedInRateLimited(LinkedInError):
    """429 — too many requests."""


class LinkedInUpstreamError(LinkedInError):
    """5xx or timeout from LinkedIn."""


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DASH_ENDPOINT = "https://www.linkedin.com/voyager/api/identity/dash/profiles"
CONTACT_ENDPOINT = "https://www.linkedin.com/voyager/api/identity/profileContactInfo"

# Pinned decoration schema. Re-verify from live traffic if it begins to 400.
DECORATION_ID = "com.linkedin.voyager.dash.deco.identity.profile.FullProfileWithEntities-93"

_HEADERS = {
    "accept": "application/vnd.linkedin.normalized+json+2.1",
    "x-restli-protocol-version": "2.0.0",
    "x-li-lang": "en_US",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
}


@dataclass
class _CacheEntry:
    value: Dict[str, Any]
    expires: float


class _TTLCache:
    """Bounded in-memory TTL cache (: bounded everything, no unbounded growth)."""

    def __init__(self, ttl: int = 3600, max_size: int = 1024) -> None:
        self._ttl = ttl
        self._max = max_size
        self._store: Dict[str, _CacheEntry] = {}

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires < time.time():
            self._store.pop(key, None)
            return None
        return entry.value

    def set(self, key: str, value: Dict[str, Any]) -> None:
        if len(self._store) >= self._max:
            # Evict the single most-expired entry (simple, bounded).
            oldest = min(self._store, key=lambda k: self._store[k].expires)
            self._store.pop(oldest, None)
        self._store[key] = _CacheEntry(value, time.time() + self._ttl)


class LinkedInVoyagerClient:
    """Thin, dependency-light Voyager API client (cookie auth, no browser)."""

    def __init__(
        self,
        cookies: Dict[str, str],
        timeout: int = 20,
        cache_ttl: int = 3600,
        max_cache: int = 1024,
    ) -> None:
        """`cookies` = the FULL linkedin.com cookie jar (name->value).

        Sending only li_at/JSESSIONID makes the request look like a leaked token
        from a non-standard client and gets the session revoked within minutes.
        The full jar + browser headers is what makes it indistinguishable from the
        real frontend (the fidelity fix for LinkedIn's fraud scoring).
        """
        if not cookies.get("li_at"):
            raise LinkedInError("li_at cookie is required.")
        self._timeout = timeout
        self._cache = _TTLCache(ttl=cache_ttl, max_size=max_cache)
        self._session = self._build_session(cookies)

    @staticmethod
    def _build_session(cookies: Dict[str, str]) -> requests.Session:
        import random

        s = curl_requests.Session(impersonate="chrome")
        # Full cookie jar — every linkedin.com cookie, not just li_at/JSESSIONID.
        for name, value in cookies.items():
            s.cookies.set(name, value, domain=".linkedin.com")

        # CSRF: LinkedIn checks csrf-token == JSESSIONID cookie (quotes stripped).
        # If no JSESSIONID in the jar (common on modern web sessions), synthesize
        # one — it is NOT validated as a real session, only matched to the header.
        jsessionid = cookies.get("JSESSIONID") or f"ajax:{random.randint(10**19, 10**20 - 1)}"
        s.cookies.set("JSESSIONID", jsessionid, domain=".linkedin.com")

        headers = dict(_HEADERS)
        headers["csrf-token"] = jsessionid.strip('"')
        # Browser-fidelity headers (close the request fingerprint gap).
        # X-Li-Track: client telemetry — LinkedIn checks this on every request.
        # Format reverse-engineered from the LinkedIn web app (voyager-web).
        import json as _json
        headers.update({
            "accept-language": "en-US,en;q=0.9",
            "origin": "https://www.linkedin.com",
            "referer": "https://www.linkedin.com/",
            "x-requested-with": "XMLHttpRequest",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "sec-ch-ua": '"Chromium";v="125", "Google Chrome";v="125", "Not.A/Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "x-li-track": _json.dumps({
                "clientVersion": "1.0.0",
                "osName": "Windows",
                "deviceFormFactor": "DESKTOP",
                "mpName": "voyager-web",
                "displayDensity": 1,
                "displayWidth": 1920,
                "displayHeight": 1080,
            }),
        })
        s.headers.update(headers)
        return s

    # -- public API --------------------------------------------------------
    def get_profile(self, slug: str) -> Dict[str, Any]:
        """Return a normalized profile dict for a public_id/slug."""
        slug = self._normalize_slug(slug)
        cached = self._cache.get(slug)
        if cached is not None:
            return cached

        primary = self._fetch_dash(slug)
        contact = self._fetch_contact(slug)

        result = {
            "basic": self._extract_basic(primary),
            "about": primary.get("summary", "") or "",
            "experience": self._extract_by_type(primary.get("_included", []), "Position",
                                                 self._map_position),
            "education": self._extract_by_type(primary.get("_included", []), "Education",
                                               self._map_education),
            "skills": self._extract_by_type(primary.get("_included", []), "Skill",
                                            self._map_skill),
            "certifications": self._extract_by_type(primary.get("_included", []), "Certification",
                                                    self._map_cert),
            "languages": self._extract_by_type(primary.get("_included", []), "Language",
                                               self._map_language),
            "profile_images": self._extract_images(primary),
            "contact_info": contact,
        }
        self._cache.set(slug, result)
        return result

    # -- request layer ------------------------------------------------------
    def _request(self, url: str, params: Dict[str, str]) -> Dict[str, Any]:
        last_err: Optional[Exception] = None
        for attempt in range(2):  # one retry on transient upstream errors
            try:
                r = self._session.get(url, params=params, timeout=self._timeout,
                                      allow_redirects=False)
            except Exception as e:
                if "timed out" in str(e).lower():
                    raise LinkedInUpstreamError(f"Request timed out: {e}") from e
                raise LinkedInUpstreamError(f"Network error: {e}") from e

            if r.status_code == 200:
                # LinkedIn often returns 200 with an HTML bot-check / soft-block
                # page (not JSON), or 200 + JSON carrying an `exception` key, or
                # a dash response with no `included`. Treat these as failures so
                # they don't masquerade as a successful parse.
                try:
                    data = r.json()
                except ValueError:
                    raise LinkedInUpstreamError(
                        "LinkedIn returned a non-JSON 200 response (likely a "
                        "bot-check / soft-block page). Refresh the full cookie jar."
                    )
                if isinstance(data, dict) and data.get("exception"):
                    raise LinkedInAuthError(
                        "LinkedIn returned an exception payload (session invalid "
                        "or blocked). Refresh li_at + full cookie jar."
                    )
                # The dash endpoint always returns `included` on success. A 200
                # without it is a soft-block / schema break, not a real profile —
                # surface as auth so the caller refreshes cookies rather than
                # misreporting "not found".
                if (url == DASH_ENDPOINT and isinstance(data, dict)
                        and "included" not in data):
                    raise LinkedInAuthError(
                        "LinkedIn returned a 200 with no `included` (possible "
                        "soft-block or decoration schema change). Refresh the "
                        "full cookie jar."
                    )
                return data
            # LinkedIn bounces invalid/expired sessions with a 302 back to the
            # same endpoint (self-redirect loop) instead of a clean 401. With a
            # valid session it returns 200 directly, so any 3xx here means the
            # cookie is dead. Fail cleanly rather than following 30 hops.
            if r.status_code in (301, 302, 303, 307, 308):
                raise LinkedInAuthError(
                    "LinkedIn redirected (session invalid) — cookies expired or "
                    "invalid. Refresh li_at + JSESSIONID from a logged-in browser."
                )
            if r.status_code in (401, 403):
                raise LinkedInAuthError(
                    "LinkedIn returned 401/403 — cookies expired or invalid. "
                    "Refresh li_at + JSESSIONID from a logged-in browser session."
                )
            if r.status_code == 429:
                raise LinkedInRateLimited("LinkedIn returned 429 — rate limited.")
            if r.status_code == 404:
                raise LinkedInNotFound("Profile not found or private.")
            # 5xx or anything else: retry once, then surface as upstream error
            last_err = RuntimeError(f"status={r.status_code}")
            time.sleep(1.0 * (attempt + 1))
        raise LinkedInUpstreamError(f"LinkedIn upstream error: {last_err}")

    def _fetch_dash(self, slug: str) -> Dict[str, Any]:
        data = self._request(DASH_ENDPOINT, {
            "q": "memberIdentity",
            "memberIdentity": slug,
            "decorationId": DECORATION_ID,
        })
        included = data.get("included", [])
        prof = self._find_profile(included)
        if prof is None:
            # Some profiles expose publicIdentifier instead of fsd_profile detection.
            prof = self._find_profile_fallback(included, slug)
        if prof is None:
            raise LinkedInNotFound(f"Could not find profile data for slug '{slug}'.")
        prof["_included"] = included  # stash for downstream extraction
        return prof

    def _fetch_contact(self, slug: str) -> Dict[str, Any]:
        try:
            data = self._request(CONTACT_ENDPOINT, {
                "q": "memberIdentity",
                "memberIdentity": slug,
            })
        except LinkedInNotFound:
            return {}
        except LinkedInError:
            # Contact endpoint is gated; absence is expected, not fatal.
            return {}
        return self._extract_contact(data)

    # -- parsing helpers ----------------------------------------------------
    @staticmethod
    def _normalize_slug(target: str) -> str:
        t = target.strip().strip("/")
        if "linkedin.com" not in t:
            return t
        import re
        m = re.search(r"in/([^/]+)", t)
        if m:
            return m.group(1)
        m = re.search(r"([^/]+)$", t)
        return m.group(1) if m else t

    @staticmethod
    def _find_profile(included: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        for item in included:
            urn = item.get("entityUrn", "")
            if "fsd_profile:" in urn and item.get("firstName"):
                return item
        return None

    @staticmethod
    def _find_profile_fallback(included: List[Dict[str, Any]], slug: str) -> Optional[Dict[str, Any]]:
        for item in included:
            if item.get("firstName") and (item.get("publicIdentifier") == slug
                                          or item.get("publicId") == slug):
                return item
        for item in included:
            if item.get("firstName") and item.get("headline"):
                return item
        return None

    @staticmethod
    def _extract_by_type(included: List[Dict[str, Any]], suffix: str, mapper) -> List[Dict[str, Any]]:
        out = []
        for item in included:
            t = item.get("$type", "")
            if t.endswith(suffix):
                out.append(mapper(item))
        return out

    @staticmethod
    def _map_position(item: Dict[str, Any]) -> Dict[str, Any]:
        dr = item.get("dateRange", {}) or {}
        start = dr.get("start", {}) or {}
        end = dr.get("end", {}) or {}
        return {
            "title": item.get("title", ""),
            "company": item.get("companyName", ""),
            "company_urn": item.get("companyUrn", ""),
            "location": item.get("locationName", ""),
            "description": item.get("description", "") or "",
            "start_date": LinkedInVoyagerClient._fmt_date(start),
            "end_date": LinkedInVoyagerClient._fmt_date(end) if end else "Present",
            "is_current": not bool(end),
        }

    @staticmethod
    def _map_education(item: Dict[str, Any]) -> Dict[str, Any]:
        dr = item.get("dateRange", {}) or {}
        return {
            "school": item.get("schoolName", ""),
            "degree": item.get("degreeName", ""),
            "field_of_study": item.get("fieldOfStudy", ""),
            "description": item.get("description", "") or "",
            "start_year": (dr.get("start", {}) or {}).get("year"),
            "end_year": (dr.get("end", {}) or {}).get("year"),
        }

    @staticmethod
    def _map_skill(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": item.get("name", ""),
            "endorsement_count": item.get("endorsementCount", 0) or 0,
        }

    @staticmethod
    def _map_cert(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "name": item.get("name", ""),
            "authority": item.get("authority", ""),
            "license_number": item.get("licenseNumber", ""),
            "url": item.get("url", ""),
        }

    @staticmethod
    def _map_language(item: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "language": item.get("name", ""),
            "proficiency": item.get("proficiency", ""),
        }

    @staticmethod
    def _extract_basic(prof: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "first_name": prof.get("firstName", ""),
            "last_name": prof.get("lastName", ""),
            "full_name": f"{prof.get('firstName', '')} {prof.get('lastName', '')}".strip(),
            "headline": prof.get("headline", "") or "",
            "location": prof.get("locationName", "") or "",
            "industry": prof.get("industryName", "") or "",
            "public_id": prof.get("publicIdentifier") or prof.get("publicId", ""),
            "member_id": prof.get("memberId", ""),
            "profile_url": f"https://www.linkedin.com/in/{prof.get('publicIdentifier') or prof.get('publicId', '')}",
        }

    @staticmethod
    def _extract_images(prof: Dict[str, Any]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        pic = prof.get("profilePicture", {}) or {}
        if pic:
            vi = pic.get("vectorImage", {}) or {}
            if vi.get("rootUrl"):
                # Build a usable URL from artifacts (first artifact) when present.
                arts = vi.get("artifacts", []) or []
                if arts and arts[0].get("fileIdentifyingUrlPathSegment"):
                    out["profile_picture"] = vi["rootUrl"] + arts[0]["fileIdentifyingUrlPathSegment"]
                else:
                    out["profile_picture"] = vi["rootUrl"]
        cover = prof.get("coverPhoto", {}) or {}
        if cover.get("rootUrl"):
            out["cover_photo"] = cover["rootUrl"]
        return out

    @staticmethod
    def _extract_contact(data: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"email": "", "websites": [], "twitter": ""}
        # Contact info endpoint nests data differently: data["data"] or top-level.
        root = data.get("data", data)
        out["email"] = root.get("emailAddress", "") or ""
        for site in root.get("websites", []) or []:
            out["websites"].append({"url": site.get("url", ""), "type": site.get("type", "")})
        tw = root.get("twitterHandles", []) or []
        if tw:
            out["twitter"] = tw[0].get("name", "")
        return out

    @staticmethod
    def _fmt_date(d: Dict[str, Any]) -> str:
        if not d:
            return ""
        year = d.get("year")
        month = d.get("month")
        if year and month:
            try:
                months = ["", "January", "February", "March", "April", "May", "June",
                          "July", "August", "September", "October", "November", "December"]
                return f"{months[int(month)]} {year}"
            except (ValueError, IndexError, TypeError):
                return f"{month}/{year}"
        if year:
            return str(year)
        return ""
