"""Persistent session storage implementation.

Replaces the in-memory session storage with SQLite persistence.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

import structlog

from ..claude.session import ClaudeSession, SessionStorage
from .database import DatabaseManager
from .models import SessionModel

logger = structlog.get_logger()


_VALID_TABLE_NAMES = {"sessions", "cursor_sessions"}


class SQLiteSessionStorage(SessionStorage):
    """SQLite-based session storage."""

    def __init__(self, db_manager: DatabaseManager, table_name: str = "sessions"):
        """Initialize with database manager and target table name.

        ``table_name`` lets different backends (Claude, Cursor) keep their
        sessions in separate tables so resumable-session lookup doesn't
        accidentally pick up another backend's UUID.
        """
        if table_name not in _VALID_TABLE_NAMES:
            raise ValueError(
                f"Invalid session table name: {table_name!r}. "
                f"Allowed: {sorted(_VALID_TABLE_NAMES)}"
            )
        self.db_manager = db_manager
        self.table_name = table_name

    async def ensure_table(self) -> None:
        """Create the backing table if it doesn't exist.

        Idempotent. The schema mirrors the canonical ``sessions`` table
        defined in ``database.py`` but without the FOREIGN KEY constraint
        on ``user_id`` so the secondary table can live alongside the
        primary one without ordering issues during init.
        """
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {self.table_name} (
                    session_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    project_path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_cost REAL DEFAULT 0.0,
                    total_turns INTEGER DEFAULT 0,
                    message_count INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT TRUE
                )
                """
            )
            await conn.commit()

    async def _ensure_user_exists(
        self, user_id: int, username: Optional[str] = None
    ) -> None:
        """Ensure user exists in database before creating session."""
        async with self.db_manager.get_connection() as conn:
            # Check if user exists
            cursor = await conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?", (user_id,)
            )
            user_exists = await cursor.fetchone()

            if not user_exists:
                # Create user record
                now = datetime.now(UTC)
                await conn.execute(
                    """
                    INSERT INTO users
                    (user_id, telegram_username, first_seen, last_active, is_allowed)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        username,
                        now,
                        now,
                        True,
                    ),  # Allow user by default for now
                )
                await conn.commit()

                logger.info(
                    "Created user record for session",
                    user_id=user_id,
                    username=username,
                )

    async def save_session(self, session: ClaudeSession) -> None:
        """Save session to database."""
        # Ensure user exists before creating session
        await self._ensure_user_exists(session.user_id)

        session_model = SessionModel(
            session_id=session.session_id,
            user_id=session.user_id,
            project_path=str(session.project_path),
            created_at=session.created_at,
            last_used=session.last_used,
            total_cost=session.total_cost,
            total_turns=session.total_turns,
            message_count=session.message_count,
        )

        async with self.db_manager.get_connection() as conn:
            # Try to update first
            cursor = await conn.execute(
                f"""
                UPDATE {self.table_name}
                SET last_used = ?, total_cost = ?, total_turns = ?, message_count = ?
                WHERE session_id = ?
            """,
                (
                    session_model.last_used,
                    session_model.total_cost,
                    session_model.total_turns,
                    session_model.message_count,
                    session_model.session_id,
                ),
            )

            # If no rows were updated, insert new record
            if cursor.rowcount == 0:
                await conn.execute(
                    f"""
                    INSERT INTO {self.table_name}
                    (session_id, user_id, project_path, created_at, last_used,
                     total_cost, total_turns, message_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        session_model.session_id,
                        session_model.user_id,
                        session_model.project_path,
                        session_model.created_at,
                        session_model.last_used,
                        session_model.total_cost,
                        session_model.total_turns,
                        session_model.message_count,
                    ),
                )

            await conn.commit()

        logger.debug(
            "Session saved to database",
            session_id=session.session_id,
            user_id=session.user_id,
        )

    async def load_session(
        self, session_id: str, user_id: int
    ) -> Optional[ClaudeSession]:
        """Load session from database, filtered by user ownership."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT * FROM {self.table_name} WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            )
            row = await cursor.fetchone()

            if not row:
                return None

            session_model = SessionModel.from_row(row)

            # Convert to ClaudeSession
            claude_session = ClaudeSession(
                session_id=session_model.session_id,
                user_id=session_model.user_id,
                project_path=Path(session_model.project_path),
                created_at=session_model.created_at,
                last_used=session_model.last_used,
                total_cost=session_model.total_cost,
                total_turns=session_model.total_turns,
                message_count=session_model.message_count,
                tools_used=[],  # Tools are tracked separately in tool_usage table
            )

            logger.debug(
                "Session loaded from database",
                session_id=session_id,
                user_id=claude_session.user_id,
            )

            return claude_session

    async def delete_session(self, session_id: str) -> None:
        """Delete session from database."""
        async with self.db_manager.get_connection() as conn:
            await conn.execute(
                f"UPDATE {self.table_name} SET is_active = FALSE WHERE session_id = ?",
                (session_id,),
            )
            await conn.commit()

        logger.debug("Session marked as inactive", session_id=session_id)

    async def get_user_sessions(self, user_id: int) -> List[ClaudeSession]:
        """Get all active sessions for a user."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                f"""
                SELECT * FROM {self.table_name}
                WHERE user_id = ? AND is_active = TRUE
                ORDER BY last_used DESC
            """,
                (user_id,),
            )
            rows = await cursor.fetchall()

            sessions = []
            for row in rows:
                session_model = SessionModel.from_row(row)
                claude_session = ClaudeSession(
                    session_id=session_model.session_id,
                    user_id=session_model.user_id,
                    project_path=Path(session_model.project_path),
                    created_at=session_model.created_at,
                    last_used=session_model.last_used,
                    total_cost=session_model.total_cost,
                    total_turns=session_model.total_turns,
                    message_count=session_model.message_count,
                    tools_used=[],  # Tools are tracked separately
                )
                sessions.append(claude_session)

            return sessions

    async def get_all_sessions(self) -> List[ClaudeSession]:
        """Get all active sessions."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                f"SELECT * FROM {self.table_name} WHERE is_active = TRUE ORDER BY last_used DESC"
            )
            rows = await cursor.fetchall()

            sessions = []
            for row in rows:
                session_model = SessionModel.from_row(row)
                claude_session = ClaudeSession(
                    session_id=session_model.session_id,
                    user_id=session_model.user_id,
                    project_path=Path(session_model.project_path),
                    created_at=session_model.created_at,
                    last_used=session_model.last_used,
                    total_cost=session_model.total_cost,
                    total_turns=session_model.total_turns,
                    message_count=session_model.message_count,
                    tools_used=[],  # Tools are tracked separately
                )
                sessions.append(claude_session)

            return sessions

    async def cleanup_expired_sessions(self, timeout_hours: int) -> int:
        """Mark expired sessions as inactive."""
        async with self.db_manager.get_connection() as conn:
            cursor = await conn.execute(
                """
                UPDATE sessions
                SET is_active = FALSE
                WHERE last_used < datetime('now', '-' || ? || ' hours')
                  AND is_active = TRUE
            """,
                (timeout_hours,),
            )
            await conn.commit()

            affected = cursor.rowcount
            logger.info(
                "Cleaned up expired sessions",
                count=affected,
                timeout_hours=timeout_hours,
            )
            return affected
