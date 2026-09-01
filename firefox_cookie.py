"""
firefox_cookie.py — read LinkedIn cookies directly from the local Firefox
profile on THIS machine (where the LinkedIn browser session lives).

Returns the FULL cookie jar (all linkedin.com cookies), not just li_at. LinkedIn
binds a session to its complete cookie set + browser context; sending only
li_at/JSESSIONID makes the request look like a leaked token from a non-standard
client and gets the session revoked. Sending the whole jar is the fidelity fix.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time


_DEFAULT_PROFILE = ""  # dev must set FIREFOX_PROFILE_DIR; empty is safe on headless hosts


def _profile_dir() -> str:
    return os.environ.get("FIREFOX_PROFILE_DIR", _DEFAULT_PROFILE)


def get_all_linkedin_cookies() -> dict[str, str]:
    """Return {name: value} for every linkedin.com cookie, or {}."""
    profile = _profile_dir()
    live = os.path.join(profile, "cookies.sqlite")
    if not os.path.exists(live):
        return {}

    tmp = os.path.join(tempfile.gettempdir(), f"ff_ck_{os.getpid()}.sqlite")
    for _ in range(5):
        try:
            shutil.copyfile(live, tmp)
            break
        except (OSError, sqlite3.OperationalError):
            time.sleep(0.3)
    else:
        return {}

    try:
        src = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        dst = sqlite3.connect(f"file:{tmp}.bk?mode=rwc", uri=True)
        try:
            src.backup(dst)
        except sqlite3.OperationalError:
            pass
        rows = dst.execute(
            "SELECT name, value FROM moz_cookies WHERE host LIKE '%linkedin.com%'"
        ).fetchall()
        dst.close()
        src.close()
        return {name: value for name, value in rows}
    except sqlite3.OperationalError:
        return {}
    finally:
        for p in (tmp, f"{tmp}.bk"):
            try:
                os.remove(p)
            except OSError:
                pass


def get_li_at() -> str | None:
    return get_all_linkedin_cookies().get("li_at")
