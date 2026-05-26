"""Cursor Agent CLI integration."""

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

import structlog

from src.config.settings import Settings
from src.security.validators import SecurityValidator
from src.claude.sdk_integration import ClaudeResponse, StreamUpdate
from src.claude.exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)

logger = structlog.get_logger()

# Fallback message when Cursor produces no text but did use tools.
TASK_COMPLETED_MSG = "✅ Task completed. Tools used: {tools_summary}"


class CursorAgentManager:
    """Manage Cursor Agent CLI integration."""

    def __init__(
        self,
        config: Settings,
        security_validator: Optional[SecurityValidator] = None,
    ):
        """Initialize Cursor manager with configuration."""
        self.config = config
        self.security_validator = security_validator

        # Set up environment for Cursor CLI if needed
        # Cursor uses its own authentication system (cursor-agent login)
        logger.info("Cursor Agent Manager initialized")

    def _is_retryable_error(self, exc: BaseException) -> bool:
        """Return True for transient errors that warrant a retry."""
        # For now, we'll treat most connection errors as retryable
        # This can be refined based on actual Cursor CLI error patterns
        return isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError))

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> ClaudeResponse:
        """Execute Cursor Agent command via CLI."""
        start_time = asyncio.get_event_loop().time()

        logger.info(
            "Starting Cursor Agent command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        try:
            # Build Cursor Agent command
            cmd = ["cursor-agent"]

            # Add headless/non-interactive flags
            cmd.append("--print")  # Non-interactive mode

            # Add output format for streaming
            cmd.extend(["--output-format", "stream-json"])

            # Add working directory
            cmd.extend(["--cwd", str(working_directory)])

            # Handle session resumption
            if session_id and continue_session:
                # For Cursor, we assume session_id is the chatID to resume
                cmd.extend(["--resume", session_id])
                logger.info("Resuming previous Cursor session", session_id=session_id)

            # Add the prompt
            cmd.append(prompt)

            # Prepare environment
            env = os.environ.copy()
            # Cursor CLI might need specific env vars, but typically uses login state

            # Execute the command
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(working_directory),
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                limit=1024 * 1024,  # 1MB limit
            )

            # Collect messages and handle streaming
            messages: List[Dict[str, Any]] = []
            interrupted = False
            tools_used: List[Dict[str, Any]] = []
            cursor_session_id = session_id or ""
            result_content = None
            cost = 0.0

            async def _read_stdout():
                """Read and parse stdout line by line (stderr merged into stdout)."""
                assert process.stdout is not None
                while True:
                    line = await process.stdout.readline()
                    if not line:
                        break
                    try:
                        line_str = line.decode("utf-8").rstrip("\n\r")
                        if line_str:
                            # Parse stream-json line
                            event = json.loads(line_str)
                            messages.append(event)

                            # Handle streaming callback
                            if stream_callback:
                                try:
                                    await self._handle_cursor_stream_event(
                                        event, stream_callback, tools_used
                                    )
                                except Exception as callback_error:
                                    logger.warning(
                                        "Stream callback failed",
                                        error=str(callback_error),
                                        error_type=type(callback_error).__name__,
                                    )
                    except json.JSONDecodeError as e:
                        logger.debug(
                            "Skipping non-JSON line from Cursor output",
                            line=line_str[:100],
                            error=str(e),
                        )
                    except Exception as e:
                        logger.warning(
                            "Error processing Cursor stdout line",
                            error=str(e),
                        )

            # Start reading stdout (stderr is merged into stdout)
            stdout_task = asyncio.create_task(_read_stdout())

            # Execute with timeout and retry, racing against optional interrupt
            max_attempts = max(1, getattr(self.config, 'cursor_retry_max_attempts', 3))
            last_exc: Optional[BaseException] = None

            for attempt in range(max_attempts):
                if attempt > 0:
                    delay = min(
                        getattr(self.config, 'cursor_retry_base_delay', 1.0)
                        * (getattr(self.config, 'cursor_retry_backoff_factor', 2.0) ** (attempt - 1)),
                        getattr(self.config, 'cursor_retry_max_delay', 10.0),
                    )
                    logger.warning(
                        "Retrying Cursor Agent command",
                        attempt=attempt + 1,
                        max_attempts=max_attempts,
                        delay_seconds=delay,
                    )
                    await asyncio.sleep(delay)

                # Reset for retry
                messages.clear()
                tools_used.clear()
                interrupted = False
                cursor_session_id = session_id or ""
                result_content = None
                cost = 0.0

                # Re-create process for retry
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(working_directory),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=1024 * 1024,
                )

                # Restart readers
                stdout_task = asyncio.create_task(_read_stdout())
                stderr_task = asyncio.create_task(_read_stderr())

                interrupt_watcher: Optional["asyncio.Task[None]"] = None
                if interrupt_event is not None:

                    async def _cancel_on_interrupt() -> None:
                        nonlocal interrupted
                        await interrupt_event.wait()
                        interrupted = True
                        if process.returncode is None:
                            process.terminate()
                            try:
                                await process.wait()
                            except Exception:
                                pass

                    interrupt_watcher = asyncio.create_task(_cancel_on_interrupt())

                try:
                    # Wait for process to complete
                    returncode = await asyncio.wait_for(
                        process.wait(),
                        timeout=getattr(self.config, 'cursor_timeout_seconds', 120),
                    )

                    # Wait for readers to finish
                    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)

                    if returncode == 0 or interrupted:
                        break  # success or user interrupted
                    else:
                        # Process failed
                        raise ClaudeProcessError(f"Cursor Agent exited with code {returncode}")

                except asyncio.TimeoutError:
                    if not interrupted:
                        # Timeout - don't retry
                        process.terminate()
                        try:
                            await process.wait()
                        except Exception:
                            pass
                        raise ClaudeTimeoutError(
                            f"Cursor Agent timed out after {getattr(self.config, 'cursor_timeout_seconds', 120)}s"
                        )
                    else:
                        # User interrupted during timeout
                        process.terminate()
                        try:
                            await process.wait()
                        except Exception:
                            pass
                        break
                except Exception as exc:
                    if self._is_retryable_error(exc) and attempt < max_attempts - 1:
                        last_exc = exc
                        logger.warning(
                            "Transient error, will retry",
                            attempt=attempt + 1,
                            error=str(exc),
                        )
                        # Clean up process
                        if process.returncode is None:
                            process.terminate()
                            try:
                                await process.wait()
                            except Exception:
                                pass
                        continue
                    else:
                        # Non-retryable or attempts exhausted
                        raise
                finally:
                    if interrupt_watcher is not None:
                        interrupt_watcher.cancel()
                        try:
                            await interrupt_watcher
                        except Exception:
                            pass

            else:
                # All retries exhausted
                if last_exc is not None:
                    raise last_exc
                raise ClaudeProcessError("Cursor Agent failed after all retries")

            # Terminate reader if still running
            if not stdout_task.done():
                stdout_task.cancel()

            # Extract information from collected messages
            for message in messages:
                msg_type = message.get("type", "")

                if msg_type == "result":
                    # Final result message
                    result_content = message.get("output", "")
                    cursor_session_id = message.get("sessionId", cursor_session_id)
                    # Cursor might not provide cost/turns - use defaults or calculate
                    cost = float(message.get("cost", 0.0))
                    # num_turns might need to be calculated from message count

                elif msg_type == "tool-use":
                    # Tool usage
                    tool_name = message.get("tool", {}).get("name", "unknown")
                    tools_used.append({
                        "name": tool_name,
                        "timestamp": asyncio.get_event_loop().time(),
                        "input": message.get("tool", {}).get("input", {}),
                    })

                elif msg_type == "assistant":
                    # Assistant message content
                    if not result_content:
                        result_content = message.get("content", "")

            # Calculate duration
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # Use Cursor's session_id if available, otherwise fall back
            final_session_id = cursor_session_id or session_id or ""

            # Use result content or extract from messages
            if result_content is None:
                content_parts = []
                for message in messages:
                    if message.get("type") == "assistant":
                        content = message.get("content", "")
                        if content:
                            content_parts.append(content)
                content = "\n".join(content_parts).strip()
            else:
                content = str(result_content).strip()

            if not content and tools_used:
                tool_names = [
                    tool.get("name", "")
                    for tool in tools_used
                    if isinstance(tool.get("name"), str) and tool.get("name")
                ]
                unique_tool_names = list(dict.fromkeys(tool_names))
                tools_summary = ", ".join(unique_tool_names) or "unknown"
                content = TASK_COMPLETED_MSG.format(tools_summary=tools_summary)

            return ClaudeResponse(
                content=content,
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                num_turns=len([m for m in messages if m.get("type") in ("user", "assistant")]),
                tools_used=tools_used,
                interrupted=interrupted,
            )

        except asyncio.TimeoutError:
            logger.error(
                "Cursor Agent command timed out",
                timeout_seconds=getattr(self.config, 'cursor_timeout_seconds', 120),
            )
            raise ClaudeTimeoutError(
                f"Cursor Agent timed out after {getattr(self.config, 'cursor_timeout_seconds', 120)}s"
            )

        except Exception as e:
            logger.error("Cursor Agent error", error=str(e), error_type=type(e).__name__)
            # Map common Cursor errors to our exception types
            error_str = str(e).lower()
            if "mcp" in error_str:
                raise ClaudeMCPError(f"MCP server error: {str(e)}")
            elif "not found" in error_str or "executable" in error_str:
                raise ClaudeProcessError(
                    "Cursor Agent not found. Please ensure Cursor is installed and cursor-agent is in PATH:\n"
                    "  1. Install Cursor from https://cursor.sh\n"
                    "  2. Ensure cursor-agent is available in your PATH\n"
                    "  3. Or set CURSOR_CLI_PATH environment variable"
                )
            else:
                raise ClaudeProcessError(f"Cursor Agent error: {str(e)}")

    async def _handle_cursor_stream_event(
        self,
        event: Dict[str, Any],
        stream_callback: Callable[[StreamUpdate], None],
        tools_used: List[Dict[str, Any]],
    ) -> None:
        """Handle streaming event from Cursor Agent and convert to StreamUpdate."""
        try:
            event_type = event.get("type", "")

            if event_type == "assistant":
                # Assistant message
                content = event.get("output", "")
                tool_calls = []

                # Extract tool calls if present
                if "toolUse" in event:
                    tool_use = event["toolUse"]
                    tool_calls.append({
                        "name": tool_use.get("name", "unknown"),
                        "input": tool_use.get("input", {}),
                        "id": tool_use.get("id", ""),
                    })

                update = StreamUpdate(
                    type="assistant",
                    content=content if content else None,
                    tool_calls=tool_calls if tool_calls else None,
                )
                await stream_callback(update)

            elif event_type == "user":
                # User message
                content = event.get("output", "")
                if content:
                    update = StreamUpdate(
                        type="user",
                        content=content,
                    )
                    await stream_callback(update)

            elif event_type == "tool-result":
                # Tool result
                tool_name = event.get("tool", {}).get("name", "unknown")
                content = event.get("output", "")

                update = StreamUpdate(
                    type="tool_result",
                    content=content,
                    metadata={
                        "tool_name": tool_name,
                        "tool_output": event.get("output", {}),
                    }
                )
                await stream_callback(update)

            elif event_type == "progress":
                # Progress update
                progress_info = event.get("progress", {})
                update = StreamUpdate(
                    type="progress",
                    content=None,
                    progress=progress_info,
                )
                await stream_callback(update)

            elif event_type == "result":
                # Final result
                update = StreamUpdate(
                    type="result",
                    content=event.get("output", ""),
                    metadata={
                        "sessionId": event.get("sessionId"),
                        "totalCost": event.get("cost", 0.0),
                    }
                )
                await stream_callback(update)

            elif event_type == "error":
                # Error event
                error_msg = event.get("message", "Unknown error")
                update = StreamUpdate(
                    type="error",
                    content=error_msg,
                    metadata={"is_error": True}
                )
                await stream_callback(update)

        except Exception as e:
            logger.warning(
                "Failed to handle Cursor stream event",
                error=str(e),
                event=event,
            )
            # Don't re-raise to avoid breaking the stream