"""Fetch Cursor subscription usage from the dashboard API.

cursor-agent CLI does not expose plan/quota usage, so this reads the local
Cursor IDE session token and calls the (undocumented) dashboard endpoint the
web UI uses. It is best-effort: it returns ``None`` with a reason when the
token can't be found or the endpoint changes.
"""

import asyncio
import json
import sqlite3
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog

logger = structlog.get_logger()

USAGE_ENDPOINT = "https://cursor.com/api/dashboard/get-current-period-usage"
_HOME = Path.home()
_CLI_CONFIG = _HOME / ".cursor" / "cli-config.json"


def _state_db_candidates() -> List[Path]:
    """Known locations of the Cursor IDE global-state SQLite DB per platform."""
    rel = Path("User") / "globalStorage" / "state.vscdb"
    bases: List[Path] = []
    if sys.platform == "darwin":
        bases.append(_HOME / "Library" / "Application Support" / "Cursor")
    elif sys.platform.startswith("linux"):
        bases.append(_HOME / ".config" / "Cursor")
    elif sys.platform.startswith("win"):
        import os

        appdata = os.environ.get("APPDATA")
        if appdata:
            bases.append(Path(appdata) / "Cursor")
    return [b / rel for b in bases]


@dataclass
class CursorUsage:
    """Parsed plan-usage figures (amounts are in cents)."""

    total_spend: int
    limit: int
    total_percent_used: float
    auto_percent_used: float
    display_message: str
    raw: dict


class CursorUsageError(Exception):
    """Raised when usage cannot be retrieved."""


def _read_user_id() -> Optional[str]:
    try:
        data = json.loads(_CLI_CONFIG.read_text())
        uid = (data.get("authInfo") or {}).get("userId")
        return str(uid) if uid else None
    except Exception as e:
        logger.debug("Cursor usage: failed to read userId", error=str(e))
        return None


def _read_access_token() -> Optional[str]:
    for db_path in _state_db_candidates():
        if not db_path.exists():
            continue
        try:
            # Read-only, immutable so we don't disturb a running IDE.
            conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
            cur = conn.execute(
                "SELECT value FROM ItemTable WHERE key = 'cursorAuth/accessToken'"
            )
            row = cur.fetchone()
            conn.close()
            if row and isinstance(row[0], str) and row[0]:
                return row[0]
        except Exception as e:
            logger.debug(
                "Cursor usage: failed to read token from state db",
                db=str(db_path),
                error=str(e),
            )
    return None


def _fetch_usage_blocking() -> CursorUsage:
    user_id = _read_user_id()
    token = _read_access_token()
    if not user_id or not token:
        raise CursorUsageError(
            "Cursor session token not found. This needs Cursor IDE installed "
            "and logged in on the bot host."
        )

    cookie = urllib.parse.quote(f"{user_id}::{token}", safe="")
    req = urllib.request.Request(
        USAGE_ENDPOINT,
        data=b"{}",
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Cookie": f"WorkosCursorSessionToken={cookie}",
            "Origin": "https://cursor.com",
            "Referer": "https://cursor.com/dashboard",
            "User-Agent": "claude-code-telegram/cursor-usage",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:200]
        raise CursorUsageError(f"Cursor API returned {e.code}: {body}") from e
    except Exception as e:
        raise CursorUsageError(f"Cursor API request failed: {e}") from e

    plan = payload.get("planUsage") or {}
    if not plan:
        raise CursorUsageError("Cursor API response missing planUsage")

    return CursorUsage(
        total_spend=int(plan.get("totalSpend", 0)),
        limit=int(plan.get("limit", 0)),
        total_percent_used=float(plan.get("totalPercentUsed", 0.0)),
        auto_percent_used=float(plan.get("autoPercentUsed", 0.0)),
        display_message=str(
            payload.get("autoModelSelectedDisplayMessage")
            or payload.get("displayMessage")
            or ""
        ),
        raw=payload,
    )


async def fetch_cursor_usage() -> CursorUsage:
    """Fetch current-period Cursor usage. Raises CursorUsageError on failure."""
    return await asyncio.to_thread(_fetch_usage_blocking)
