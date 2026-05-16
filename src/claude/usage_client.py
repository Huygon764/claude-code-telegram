"""Fetch Claude subscription plan usage from the OAuth usage endpoint.

Replicates what the Claude Code CLI ``/usage`` panel does:
``GET {BASE}/api/oauth/usage`` with the local OAuth access token.

This endpoint is undocumented and reverse-engineered from the Claude Code
CLI. It may break if Anthropic changes it. The OAuth access token is read
from the same place the CLI/SDK keeps it (macOS Keychain or
``.credentials.json``) and is never logged.
"""

import asyncio
import json
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
import structlog

logger = structlog.get_logger()

_DEFAULT_BASE_URL = "https://api.anthropic.com"
_USAGE_PATH = "/api/oauth/usage"
_KEYCHAIN_SERVICE = "Claude Code-credentials"  # macOS Keychain service name
_ANTHROPIC_BETA = "oauth-2025-04-20"
_USER_AGENT = "claude-code/2.1.138"
_TIMEOUT_SECONDS = 5.0

# Window keys consumed from the endpoint, in display order. The response
# also carries other internal keys and an `extra_usage` object (not a
# `overage` window) which we deliberately ignore.
WINDOW_KEYS = (
    "five_hour",
    "seven_day",
    "seven_day_sonnet",
    "seven_day_opus",
)


class UsageError(Exception):
    """Raised when plan usage cannot be fetched."""


def _base_url() -> str:
    base = (
        os.environ.get("CLAUDE_CODE_API_BASE_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or _DEFAULT_BASE_URL
    )
    return base.rstrip("/")


async def _read_oauth_token() -> str:
    """Read the Claude OAuth access token from the local machine.

    macOS: Keychain (service ``Claude Code-credentials``).
    Otherwise / when ``CLAUDE_CONFIG_DIR`` is set:
    ``~/.claude/.credentials.json`` (or ``$CLAUDE_CONFIG_DIR/.credentials.json``).

    The token value is never logged.
    """
    raw: Optional[str] = None

    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    candidates: List[str] = []
    if config_dir:
        candidates.append(os.path.join(config_dir, ".credentials.json"))
    candidates.append(os.path.expanduser("~/.claude/.credentials.json"))

    for path in candidates:
        try:
            with open(path, "r") as fh:
                raw = fh.read()
            break
        except OSError:
            continue

    if raw is None and sys.platform == "darwin":
        try:
            proc = await asyncio.create_subprocess_exec(
                "security",
                "find-generic-password",
                "-s",
                _KEYCHAIN_SERVICE,
                "-w",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            if proc.returncode == 0:
                raw = out.decode("utf-8", "replace").strip()
        except (OSError, asyncio.TimeoutError) as exc:
            raise UsageError(f"Keychain read failed: {exc}") from exc

    if not raw:
        raise UsageError(
            "No Claude OAuth credentials found (Keychain "
            "'Claude Code-credentials' or ~/.claude/.credentials.json). "
            "Log in with the Claude Code CLI on this machine first."
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UsageError("OAuth credentials are not valid JSON") from exc

    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not token or not isinstance(token, str):
        raise UsageError(
            "OAuth credentials present but no accessToken (plan usage needs "
            "a Claude subscription login, not API-key auth)."
        )
    return str(token)


def _parse_windows(data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Map the raw ``/api/oauth/usage`` response to window dicts.

    The endpoint returns ``utilization`` already as a percentage (0..100)
    and ``resets_at`` as an ISO-8601 UTC string (e.g.
    ``2026-05-16T16:30:00.030050+00:00``). Unused windows are ``null``.

    Each parsed window: ``{"utilization": <float 0..100>,
    "resets_at": <aware datetime|None>}``.
    """
    windows: Dict[str, Dict[str, Any]] = {}
    for key in WINDOW_KEYS:
        w = data.get(key)
        if not isinstance(w, dict):
            continue
        util = w.get("utilization")
        if not isinstance(util, (int, float)):
            continue
        resets_raw = w.get("resets_at")
        resets_at: Optional[datetime] = None
        if isinstance(resets_raw, str):
            try:
                resets_at = datetime.fromisoformat(resets_raw)
            except ValueError:
                resets_at = None
        windows[key] = {
            "utilization": float(util),
            "resets_at": resets_at,
        }
    return windows


async def fetch_plan_usage() -> Dict[str, Any]:
    """Fetch Claude subscription plan usage.

    Returns ``{"windows": {key: {...}}}``. Raises :class:`UsageError` on any
    failure (no silent fallback).
    """
    token = await _read_oauth_token()
    url = f"{_base_url()}{_USAGE_PATH}"
    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-beta": _ANTHROPIC_BETA,
        "Content-Type": "application/json",
        "User-Agent": _USER_AGENT,
    }
    # Mirror src/bot/core.py: honour HTTPS_PROXY/HTTP_PROXY explicitly and
    # disable httpx env autodetection (avoids picking up a SOCKS ALL_PROXY
    # which would require the optional socksio dependency).
    client_kwargs: Dict[str, Any] = {
        "timeout": _TIMEOUT_SECONDS,
        "trust_env": False,
    }
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    if proxy:
        client_kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise UsageError(f"Request to usage endpoint failed: {exc}") from exc

    if resp.status_code == 401:
        raise UsageError(
            "OAuth token rejected (401) — re-login with the Claude Code CLI."
        )
    if resp.status_code != 200:
        raise UsageError(f"Usage endpoint returned HTTP {resp.status_code}")

    try:
        data = resp.json()
    except ValueError as exc:
        raise UsageError("Usage endpoint returned non-JSON response") from exc

    if not isinstance(data, dict):
        raise UsageError("Unexpected usage response shape")

    windows = _parse_windows(data)
    if not windows:
        raise UsageError("Usage response contained no recognised windows")
    return {"windows": windows}
