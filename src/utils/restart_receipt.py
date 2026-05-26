"""Cross-restart notification receipt.

``/restart`` and ``/deploy`` SIGTERM the process; a supervisor brings it
back as a brand-new process with no memory of who asked. To close the
loop ("bot is back online") we drop a small receipt on disk *before*
exiting and read it back once on startup.

The receipt is intentionally a tiny standalone JSON file (not the
SQLite DB) so it survives the process boundary, is readable very early
in startup before storage is wired, and is trivially safe to delete.
"""

import json
import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

import structlog

from .gitinfo import bot_repo_root

if TYPE_CHECKING:
    from ..config.settings import Settings

logger = structlog.get_logger()

# A receipt older than this is treated as stale and ignored: the process
# likely died for an unrelated reason (crash, host reboot) long after the
# command, so "back online" against that old message would be misleading.
_MAX_AGE_SECONDS = 600

_FILE_NAME = ".restart_receipt.json"


def receipt_path(settings: "Settings") -> Path:
    """Resolve the receipt file path.

    Co-located with the SQLite DB (same state directory). Falls back to
    the bot repo root when the DB is not a local SQLite file.
    """
    db_path = settings.database_path
    base_dir = db_path.parent if db_path else bot_repo_root()
    return base_dir / _FILE_NAME


def write_receipt(
    settings: "Settings",
    *,
    chat_id: int,
    message_id: int,
    user_id: int,
    kind: str,
    detail: Optional[str] = None,
    message_thread_id: Optional[int] = None,
) -> None:
    """Persist who/where to notify after the imminent restart.

    Best-effort and never raises: failing to write a receipt must not
    block a restart the user explicitly asked for.

    Args:
        chat_id: Chat that triggered the restart.
        message_id: The "Restarting…" message to edit into "back online".
        user_id: Requesting user (audit/debug only).
        kind: ``"restart"`` or ``"deploy"``.
        detail: Optional one-line extra context (e.g. ``"ab12cd3 -> ef45gh6"``).
        message_thread_id: Forum/topic thread id, so the fallback message
            lands in the originating project topic (not General).
    """
    path = receipt_path(settings)
    payload: Dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "user_id": user_id,
        "kind": kind,
        "detail": detail,
        "message_thread_id": message_thread_id,
        "ts": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: write a temp file in the same dir, then os.replace.
        fd, tmp_name = tempfile.mkstemp(
            prefix=_FILE_NAME, dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp_name, path)
        except Exception:
            # Clean up the temp file on any failure before re-raising.
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        logger.warning("Failed to write restart receipt", error=str(exc))


def read_and_clear_receipt(settings: "Settings") -> Optional[Dict[str, Any]]:
    """Read the receipt, delete it, and return it if still fresh.

    The file is always removed (when present) so the notification fires
    exactly once. Returns ``None`` when there is no receipt, it is
    unreadable, or it is older than the staleness window.
    """
    path = receipt_path(settings)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("Failed to read restart receipt", error=str(exc))
        return None
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Failed to clear restart receipt", error=str(exc))

    try:
        data: Dict[str, Any] = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning("Restart receipt is corrupt", error=str(exc))
        return None

    ts = data.get("ts")
    if not isinstance(ts, (int, float)) or time.time() - ts > _MAX_AGE_SECONDS:
        logger.info("Ignoring stale restart receipt", age_ts=ts)
        return None
    return data
