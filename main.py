"""
LinkedIn Profile API — FastAPI service.

Reverse-engineered LinkedIn profile scraper. Talks directly to LinkedIn's
internal Voyager API (no browser automation). See linkedin_scraper.py for the
full reverse-engineering notes.

Run locally:  uvicorn main:app --port 8099
Service:     systemd unit (see deploy/ notes in README).
"""
from __future__ import annotations

import json
import os
import time
from typing import Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from linkedin_scraper import (
    LinkedInVoyagerClient,
    LinkedInAuthError,
    LinkedInNotFound,
    LinkedInRateLimited,
    LinkedInUpstreamError,
    LinkedInError,
)


# ---------------------------------------------------------------------------
# Load .env (thin parser — no external dep). Keys already in the environment
# win, so systemd/shell-provided values are respected.
# ---------------------------------------------------------------------------
def _load_dotenv(path=".env") -> None:
    # Resolve relative to this module file so it works regardless of uvicorn CWD.
    if not os.path.isabs(path):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.exists(path):
        return
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            os.environ[k] = v


_load_dotenv()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PORT = int(os.environ.get("PORT", "8099"))
CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*").split(",")
RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", "30"))
# Optional API key. When set, /profile requires `Authorization: Bearer <key>`
# (or `?key=<key>`). When unset the API is open (dev mode) — never run the
# public deployment without API_KEY, or anyone can drive your LinkedIn cookies.
API_KEY = os.environ.get("API_KEY", "")


def _check_auth(request: Request) -> None:
    if not API_KEY:
        return  # open/dev mode
    auth = request.headers.get("authorization", "")
    key = auth[len("Bearer "):] if auth.startswith("Bearer ") else request.query_params.get("key", "")
    if key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key.")


def _load_cookies_file() -> Dict[str, str]:
    """Full cookie jar shipped as cookies.json (gitignored).

    This is the PRIMARY production path: on a headless VPS there is no Firefox,
    so the full jar (all linkedin.com cookies, not just li_at) MUST come from
    here — otherwise the session looks like a leaked token and is revoked within
    minutes (see linkedin_scraper.py RE notes).
    """
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as fh:
            data = json.load(fh)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_cookies_env() -> Dict[str, str]:
    """Full cookie jar as a JSON string in LINKEDIN_COOKIES (deploy secret)."""
    raw = os.environ.get("LINKEDIN_COOKIES", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_client() -> Optional[LinkedInVoyagerClient]:
    """Build client from merged auth sources — the fidelity fix.

    Precedence (later overrides earlier):
      1. Firefox session jar        — dev only; absent on a headless VPS
      2. cookies.json file          — full jar, PRIMARY production path
      3. LINKEDIN_COOKIES env       — full jar, alternate deploy secret
      4. LI_AT_COOKIE env           — reliable li_at override

    Sending only li_at/JSESSIONID looks like a leaked token from a non-standard
    client → session revoked within minutes. The full jar + browser headers makes
    the request indistinguishable from the real frontend.
    """
    jar: Dict[str, str] = {}

    # 1) Dev convenience: live Firefox session on THIS machine.
    try:
        from firefox_cookie import get_all_linkedin_cookies
        jar = get_all_linkedin_cookies()
    except Exception:
        pass

    # 2) + 3) Full jar from file / env — the prod path that survives without Firefox.
    merged = dict(jar)
    merged.update(_load_cookies_file())
    merged.update(_load_cookies_env())
    jar = merged

    # 4) .env li_at is the reliable auth override.
    env_li_at = os.environ.get("LI_AT_COOKIE", "")
    if env_li_at:
        jar["li_at"] = env_li_at

    if not jar.get("li_at"):
        return None

    return LinkedInVoyagerClient(cookies=jar)


def _refresh_client() -> Optional[LinkedInVoyagerClient]:
    """Re-read the FULL cookie jar from the local Firefox session and rebuild.

    LinkedIn sessions die fast (minutes) when used from a non-standard client.
    Running on the same host as the logged-in Firefox lets us re-read the live
    cookie jar (all 25+ cookies including device-binding ones like dfpfpt, lidc,
    bcookie) without manual intervention — the fidelity fix that keeps the
    session alive.
    """
    return _load_client()  # _load_client already tries Firefox jar first


# Client is built once at startup (: reuse session, no per-request re-auth).
_CLIENT: Optional[LinkedInVoyagerClient] = _load_client()


# ---------------------------------------------------------------------------
# Pydantic response models (typed -> recruiter sees a clean, documented schema)
# ---------------------------------------------------------------------------
class Basic(BaseModel):
    first_name: str = ""
    last_name: str = ""
    full_name: str = ""
    headline: str = ""
    location: str = ""
    industry: str = ""
    public_id: str = ""
    member_id: str = ""
    profile_url: str = ""


class Experience(BaseModel):
    title: str = ""
    company: str = ""
    company_urn: str = ""
    location: str = ""
    description: str = ""
    start_date: str = ""
    end_date: str = ""
    is_current: bool = False


class Education(BaseModel):
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    description: str = ""
    start_year: Optional[int] = None
    end_year: Optional[int] = None


class Skill(BaseModel):
    name: str = ""
    endorsement_count: int = 0


class Certification(BaseModel):
    name: str = ""
    authority: str = ""
    license_number: str = ""
    url: str = ""


class Language(BaseModel):
    language: str = ""
    proficiency: str = ""


class ContactInfo(BaseModel):
    email: str = ""
    websites: list = Field(default_factory=list)
    twitter: str = ""


class ProfileImages(BaseModel):
    profile_picture: str = ""
    cover_photo: str = ""


class ProfileResponse(BaseModel):
    status: str = "success"
    source: str = "linkedin_voyager_api"
    fetched_at: str = ""
    data: Dict  # nested basic/about/experience/.../contact_info


class ErrorResponse(BaseModel):
    status: str = "error"
    error: str
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="LinkedIn Profile API",
    description=(
        "Reverse-engineered LinkedIn profile scraper. Authenticates via the "
        "internal Voyager API using session cookies — no browser automation. "
        "Returns structured JSON for a given LinkedIn profile URL or slug."
    ),
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,        # public demo API: open CORS, NO credentials
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


# Very small in-memory rate limiter (per remote IP). Bounded; demo-grade.
_RATE: Dict[str, list] = {}


def _rate_ok(remote: str) -> bool:
    now = time.time()
    hits = _RATE.get(remote, [])
    hits = [t for t in hits if now - t < 60]
    if len(hits) >= RATE_LIMIT_PER_MIN:
        _RATE[remote] = hits
        return False
    hits.append(now)
    _RATE[remote] = hits
    return True


def _get_client() -> LinkedInVoyagerClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _refresh_client()
    if _CLIENT is None:
        raise HTTPException(
            status_code=500,
            detail="Server not configured with LI_AT_COOKIE. "
                   "Set it in the environment (or log into LinkedIn in the local "
                   "Firefox session) and restart.",
        )
    return _CLIENT


def _to_http_error(e: LinkedInError) -> HTTPException:
    if isinstance(e, LinkedInAuthError):
        return HTTPException(status_code=401, detail=e.args[0])
    if isinstance(e, LinkedInNotFound):
        return HTTPException(status_code=404, detail=e.args[0])
    if isinstance(e, LinkedInRateLimited):
        return HTTPException(status_code=429, detail=e.args[0])
    if isinstance(e, LinkedInUpstreamError):
        return HTTPException(status_code=502, detail=e.args[0])
    return HTTPException(status_code=500, detail=str(e))


@app.get("/", tags=["meta"])
def root():
    return {
        "service": "LinkedIn Profile API",
        "version": "1.0.0",
        "auth_configured": _CLIENT is not None,
        "endpoints": {
            "GET /profile?url={linkedin_url}": "Fetch by full profile URL",
            "GET /profile?slug={public_id}": "Fetch by username/slug",
            "POST /profile": "Fetch by JSON body {url|slug}",
            "GET /health": "Health check",
        },
        "docs": "/docs",
    }


@app.get("/health", tags=["meta"])
def health():
    return {
        "status": "healthy",
        "auth_configured": _CLIENT is not None,
        "epoch": int(time.time()),
    }


def _fetch(target: str) -> ProfileResponse:
    global _CLIENT
    client = _get_client()
    try:
        data = client.get_profile(target)
    except LinkedInAuthError:
        # Session cookie died (LinkedIn invalidates fast). Self-heal: re-read a
        # fresh li_at from the local Firefox session and retry once.
        _CLIENT = _refresh_client()
        if _CLIENT is None:
            raise HTTPException(
                status_code=401,
                detail="LinkedIn session expired and no local Firefox session found. "
                       "Log into LinkedIn in the browser on this host.",
            )
        try:
            data = _CLIENT.get_profile(target)
        except LinkedInError as e:
            raise _to_http_error(e) from e
    except LinkedInError as e:
        raise _to_http_error(e) from e
    return ProfileResponse(
        data=data,
        fetched_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    )


@app.get("/profile", response_model=ProfileResponse, tags=["profiles"])
def get_profile_by_query(
    url: Optional[str] = Query(None, description="Full LinkedIn profile URL"),
    slug: Optional[str] = Query(None, description="LinkedIn public id / slug"),
    _: None = Depends(_check_auth),
):
    if not url and not slug:
        raise HTTPException(status_code=400, detail="Provide 'url' or 'slug'.")
    return _fetch(url or slug)


@app.post("/profile", response_model=ProfileResponse, tags=["profiles"])
def get_profile_by_body(body: dict, _: None = Depends(_check_auth)):
    target = body.get("url") or body.get("slug")
    if not target:
        raise HTTPException(status_code=400, detail="Body must contain 'url' or 'slug'.")
    return _fetch(target)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
