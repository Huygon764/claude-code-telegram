"""Git revision info for the running bot process.

Single source of truth for the commit the running process was started
from. ``STARTUP_COMMIT`` is captured once at import (process start) so it
reflects the code actually loaded in memory, not whatever HEAD happens to
be on disk later (which may be ahead after a pull-before-restart).
"""

import os
import subprocess
from pathlib import Path
from typing import List, Optional


def bot_repo_root() -> Path:
    """Locate the bot's own source repo root (the dir containing .git).

    Walks up from this file; falls back to the package parent. This is the
    bot's code repo, NOT any project a session operates on.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return parent
    # src/utils/gitinfo.py -> utils -> src -> repo root
    return here.parents[2]


def _git(args: List[str], cwd: Path) -> Optional[str]:
    """Run a read-only git command; return stripped stdout or None.

    Never prompts or hangs for credentials.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _capture_startup_commit() -> Optional[str]:
    """Full SHA of the code this process was started from."""
    return _git(["rev-parse", "HEAD"], bot_repo_root())


# Captured once, at import time (== process start).
STARTUP_COMMIT: Optional[str] = _capture_startup_commit()


def running_revision() -> str:
    """One-line description of the running revision, e.g. ``ab12cd3 (main)``.

    Best-effort; returns ``unknown`` when git metadata is unavailable.
    """
    short = (STARTUP_COMMIT or "")[:7]
    if not short:
        return "unknown"
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], bot_repo_root())
    return f"{short} ({branch})" if branch else short
