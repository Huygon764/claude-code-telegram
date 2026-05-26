"""Cursor Agent integration module."""

from src.claude.exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)
from src.cursor.cli_integration import CursorAgentManager
from src.cursor.facade import CursorIntegration
from src.cursor.session import (
    CursorSession,
    SessionManager as CursorSessionManager,
    SessionStorage,
)

__all__ = [
    # Exceptions
    "ClaudeMCPError",
    "ClaudeParsingError",
    "ClaudeProcessError",
    "ClaudeTimeoutError",
    # Main integration
    "CursorIntegration",
    # Core components
    "CursorAgentManager",
    "CursorSession",
    "CursorSessionManager",
    "SessionStorage",
]