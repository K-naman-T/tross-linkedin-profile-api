"""
End-to-end API test — no network, no cookies.

Stubs the LinkedIn client and drives the real FastAPI endpoint functions so we
prove the full request -> ProfileResponse wiring (auth gate, parsing, shaping)
without a live LinkedIn session. Run:
    pytest test_api_e2e.py -q
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class _FakeClient:
    """Mimics LinkedInVoyagerClient.get_profile with a Voyager-shaped payload."""

    def get_profile(self, slug: str) -> dict:
        import re
        m = re.search(r"in/([^/]+)", slug)
        norm = m.group(1) if m else slug
        return {
            "basic": {
                "first_name": "Ada", "last_name": "Lovelace",
                "full_name": "Ada Lovelace", "headline": "Mathematician",
                "location": "London", "industry": "Software",
                "public_id": norm, "member_id": "AB00",
                "profile_url": f"https://www.linkedin.com/in/{norm}",
            },
            "about": "First programmer.",
            "experience": [{"title": "Engineer", "company": "Analytical Engine",
                            "is_current": True}],
            "education": [{"school": "Uni", "degree": "BS"}],
            "skills": [{"name": "Math", "endorsement_count": 12}],
            "certifications": [{"name": "Cert"}],
            "languages": [{"language": "English", "proficiency": "Native"}],
            "profile_images": {"profile_picture": "https://x/p.jpg"},
            "contact_info": {"email": "", "websites": [], "twitter": ""},
        }


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(main, "_get_client", lambda: _FakeClient())


def test_get_profile_query(patched):
    resp = main.get_profile_by_query(url=None, slug="ada")
    assert resp.status == "success"
    assert resp.data["basic"]["full_name"] == "Ada Lovelace"
    assert resp.data["experience"][0]["company"] == "Analytical Engine"
    assert resp.source == "linkedin_voyager_api"


def test_get_profile_by_url(patched):
    resp = main.get_profile_by_query(url="https://www.linkedin.com/in/ada/")
    assert resp.data["basic"]["public_id"] == "ada"


def test_post_profile_body(patched):
    resp = main.get_profile_by_body({"url": "https://www.linkedin.com/in/ada"})
    assert resp.status == "success"
    assert resp.data["skills"][0]["name"] == "Math"


def test_missing_target():
    with pytest.raises(main.HTTPException):
        main.get_profile_by_query(url=None, slug=None)


# --- auth gate -------------------------------------------------------------
class _FakeReq:
    def __init__(self, auth=None, key=None):
        self.headers = {"authorization": auth} if auth else {}
        self.query_params = {"key": key} if key else {}


def test_auth_open_when_no_key(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "")
    main._check_auth(_FakeReq())  # no raise


def test_auth_rejects_missing_key(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "secret")
    with pytest.raises(main.HTTPException):
        main._check_auth(_FakeReq())


def test_auth_accepts_bearer(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "secret")
    main._check_auth(_FakeReq(auth="Bearer secret"))  # no raise


def test_auth_accepts_query_key(monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "secret")
    main._check_auth(_FakeReq(key="secret"))  # no raise


# --- real HTTP through the ASGI app (middleware + auth dependency + routing) -----
def test_http_get_profile(patched, monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "")
    client = TestClient(main.app)
    r = client.get("/profile?slug=ada")
    assert r.status_code == 200
    assert r.json()["data"]["basic"]["full_name"] == "Ada Lovelace"
    assert r.json()["source"] == "linkedin_voyager_api"


def test_http_auth_enforced(patched, monkeypatch):
    monkeypatch.setattr(main, "API_KEY", "secret")
    client = TestClient(main.app)
    assert client.get("/profile?slug=ada").status_code == 401
    ok = client.get("/profile?slug=ada", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
    assert ok.json()["data"]["basic"]["full_name"] == "Ada Lovelace"
