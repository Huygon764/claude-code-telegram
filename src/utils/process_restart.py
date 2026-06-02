"""Self-restart for /restart and /deploy commands.

These commands SIGTERM the process so asyncio can shut down sockets and
polling cleanly. ``reexec_if_requested()`` is called at the very end of
``run()`` in ``src/main.py``; if a restart was requested it replaces the
current process with a fresh copy of itself via ``os.execv``.

This lets ``/restart`` and ``/deploy`` work in plain ``make run`` (no
external supervisor). Under a supervisor (systemd, launchd,
``scripts/run-forever.sh``) the exec is harmless — the supervisor sees
the same PID and keeps waiting on it.
"""

import os
import sys

import structlog

logger = structlog.get_logger()

_restart_requested = False


def request_self_restart() -> None:
    """Mark this process to re-exec after the current asyncio shutdown."""
    global _restart_requested
    _restart_requested = True


def is_restart_requested() -> bool:
    return _restart_requested


def reexec_if_requested() -> None:
    """Re-exec the current entry point when a restart was requested.

    Must be called after asyncio has fully shut down (sockets closed,
    polling stopped, DB committed). Never returns on success — this
    process is replaced in place.
    """
    if not _restart_requested:
        return

    script = sys.argv[0]
    if script and os.path.isfile(script) and os.access(script, os.X_OK):
        program = script
        argv = list(sys.argv)
    else:
        # argv[0] may not be a runnable script (e.g. when launched via
        # ``python -m src.main``); fall back to the same module entry.
        program = sys.executable
        argv = [sys.executable, "-m", "src.main", *sys.argv[1:]]

    logger.info("Re-execing for self-restart", program=program)
    os.execv(program, argv)
