"""Cursor Agent CLI integration."""

import asyncio
import json
import os
import signal
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from src.claude.exceptions import (
    ClaudeMCPError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)
from src.claude.sdk_integration import ClaudeResponse, StreamUpdate
from src.config.settings import Settings
from src.security.validators import SecurityValidator

logger = structlog.get_logger()

# Fallback message when Cursor produces no text but did use tools.
TASK_COMPLETED_MSG = "✅ Task completed. Tools used: {tools_summary}"


def _extract_cursor_tool_name(tool_call: Dict[str, Any]) -> str:
    """Derive a tool name from a cursor-agent ``tool_call`` payload.

    cursor-agent encodes the tool as the key, e.g. ``{"readToolCall": {...}}``
    or ``{"editToolCall": {...}}``. Strip the ``ToolCall`` suffix to get a
    short, stable name (``read``, ``edit``, ...).
    """
    for key in tool_call.keys():
        if key.endswith("ToolCall"):
            return key[: -len("ToolCall")] or "unknown"
    return "unknown"


def _extract_cursor_message_text(message: Dict[str, Any]) -> str:
    """Pull text content out of a cursor-agent assistant/user message.

    Schema (verified against ``cursor-agent --output-format stream-json``):
        {"type":"assistant","message":{"role":"assistant",
          "content":[{"type":"text","text":"..."}]}, ...}
    """
    msg = message.get("message") or {}
    content = msg.get("content") or []
    parts: List[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text = item.get("text") or ""
                if text:
                    parts.append(text)
    return "".join(parts)


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

    async def _terminate_process_tree(
        self, process: asyncio.subprocess.Process
    ) -> None:
        """Kill cursor-agent and any child processes it spawned.

        cursor-agent forks children that ignore SIGTERM sent to the parent,
        and ``process.wait()`` will hang until the pipes drain. We send the
        signal to the whole process group, then bound the wait so this can
        never block forever.
        """
        if process.returncode is not None:
            return

        pid = process.pid

        def _signal_group(sig: int) -> None:
            try:
                os.killpg(os.getpgid(pid), sig)
            except (ProcessLookupError, PermissionError):
                # Fall back to single-process signal
                try:
                    if sig == signal.SIGTERM:
                        process.terminate()
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass

        _signal_group(signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
            return
        except asyncio.TimeoutError:
            logger.warning("SIGTERM timed out, sending SIGKILL", pid=pid)

        _signal_group(signal.SIGKILL)
        try:
            await asyncio.wait_for(process.wait(), timeout=3)
        except asyncio.TimeoutError:
            logger.warning(
                "Cursor process did not exit after SIGKILL; abandoning wait",
                pid=pid,
            )

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
            # Build Cursor Agent command.
            # Note: working_directory is set via subprocess ``cwd=`` below — the
            # CLI does not accept a ``--cwd`` flag (it errors out). We also pass
            # ``--trust`` so headless mode doesn't block on workspace approval.
            cmd = [
                "cursor-agent",
                "--print",
                "--output-format",
                "stream-json",
                "--trust",
            ]

            # Optional model override
            model = self.config.cursor_model
            if model:
                cmd.extend(["--model", str(model)])

            # Handle session resumption
            if session_id and continue_session:
                # session_id is the chatId returned in a previous ``result`` event
                cmd.extend(["--resume", session_id])
                logger.info("Resuming previous Cursor session", session_id=session_id)

            # Prompt must be the last positional argument
            cmd.append(prompt)

            # Prepare environment (Cursor CLI typically reuses login state)
            env = os.environ.copy()

            if images:
                # cursor-agent CLI has no documented image attachment mechanism;
                # surface this clearly rather than silently dropping the input.
                logger.warning(
                    "Image inputs are not supported by cursor-agent CLI; ignoring",
                    image_count=len(images),
                )

            # Shared mutable state — reset at the start of each retry attempt
            messages: List[Dict[str, Any]] = []
            tools_used: List[Dict[str, Any]] = []
            interrupted = False
            cursor_session_id = session_id or ""
            result_content: Optional[str] = None
            cost = 0.0
            process: Optional[asyncio.subprocess.Process] = None

            got_result = False

            async def _read_stdout(proc: asyncio.subprocess.Process) -> None:
                """Read and parse stdout line by line (stderr merged into stdout)."""
                nonlocal got_result
                assert proc.stdout is not None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    line_str = ""
                    try:
                        line_str = line.decode("utf-8", errors="replace").rstrip("\n\r")
                        if not line_str:
                            continue
                        event = json.loads(line_str)
                        messages.append(event)

                        logger.debug(
                            "Cursor raw event",
                            event_type=event.get("type"),
                            subtype=event.get("subtype"),
                        )

                        # Break BEFORE invoking stream_callback for the result
                        # event. The callback may do slow Telegram edits, and
                        # the orchestrator will display the final answer
                        # anyway — we don't need to push the result through
                        # the progress-message stream.
                        if event.get("type") == "result":
                            got_result = True
                            break

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
                    except json.JSONDecodeError:
                        # cursor-agent may emit a banner/error on stderr (merged
                        # into stdout) that isn't valid JSON. Log at debug.
                        logger.debug(
                            "Non-JSON line from Cursor output",
                            line=line_str[:200],
                        )
                    except Exception as e:
                        logger.warning(
                            "Error processing Cursor stdout line",
                            error=str(e),
                        )

            # Execute with timeout and retry, racing against optional interrupt
            max_attempts = max(1, self.config.cursor_retry_max_attempts)
            last_exc: Optional[BaseException] = None

            for attempt in range(max_attempts):
                if attempt > 0:
                    delay = min(
                        self.config.cursor_retry_base_delay
                        * (self.config.cursor_retry_backoff_factor ** (attempt - 1)),
                        self.config.cursor_retry_max_delay,
                    )
                    logger.warning(
                        "Retrying Cursor Agent command",
                        attempt=attempt + 1,
                        max_attempts=max_attempts,
                        delay_seconds=delay,
                    )
                    await asyncio.sleep(delay)

                # Reset state for this attempt
                messages.clear()
                tools_used.clear()
                interrupted = False
                cursor_session_id = session_id or ""
                result_content = None
                cost = 0.0

                # Spawn a fresh process for this attempt
                process = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(working_directory),
                    env=env,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    limit=1024 * 1024,
                    start_new_session=True,
                )

                stdout_task = asyncio.create_task(_read_stdout(process))

                interrupt_watcher: Optional["asyncio.Task[None]"] = None
                if interrupt_event is not None:
                    proc_ref = process

                    async def _cancel_on_interrupt() -> None:
                        nonlocal interrupted
                        await interrupt_event.wait()
                        interrupted = True
                        await self._terminate_process_tree(proc_ref)

                    interrupt_watcher = asyncio.create_task(_cancel_on_interrupt())

                try:
                    # Wait for stdout reader first — it breaks on EOF or
                    # after receiving the ``result`` event (cursor-agent may
                    # keep the process alive long after the result is sent).
                    timeout_s = self.config.cursor_timeout_seconds
                    await asyncio.wait_for(stdout_task, timeout=timeout_s)

                    # Terminate the process if it hasn't exited yet
                    # (cursor-agent often hangs after emitting the result).
                    if process.returncode is None:
                        await self._terminate_process_tree(process)
                    returncode = process.returncode or 0

                    if returncode == 0 or interrupted or got_result:
                        break  # success or user interrupted
                    else:
                        # Process failed
                        raise ClaudeProcessError(
                            f"Cursor Agent exited with code {returncode}"
                        )

                except asyncio.TimeoutError:
                    await self._terminate_process_tree(process)
                    if not interrupted:
                        raise ClaudeTimeoutError(
                            f"Cursor Agent timed out after {self.config.cursor_timeout_seconds}s"
                        )
                    else:
                        break
                except Exception as exc:
                    if self._is_retryable_error(exc) and attempt < max_attempts - 1:
                        last_exc = exc
                        logger.warning(
                            "Transient error, will retry",
                            attempt=attempt + 1,
                            error=str(exc),
                        )
                        await self._terminate_process_tree(process)
                        continue
                    else:
                        # Non-retryable or attempts exhausted
                        raise
                finally:
                    if interrupt_watcher is not None:
                        interrupt_watcher.cancel()
                        try:
                            await interrupt_watcher
                        except (asyncio.CancelledError, Exception):
                            pass

            else:
                # All retries exhausted
                if last_exc is not None:
                    raise last_exc
                raise ClaudeProcessError("Cursor Agent failed after all retries")

            # Terminate reader if still running
            if not stdout_task.done():
                stdout_task.cancel()

            # Extract information from collected messages.
            # Schema reference (verified empirically):
            #   {"type":"system","subtype":"init","session_id":"...",...}
            #   {"type":"user","message":{"content":[{"type":"text","text":"..."}]},...}
            #   {"type":"assistant","message":{"content":[{"type":"text","text":"..."}]},...}
            #   {"type":"tool_call","subtype":"started|completed",
            #     "tool_call":{"<name>ToolCall":{"args":{...},"result":{...}?}}, ...}
            #   {"type":"result","subtype":"success","result":"<final text>",
            #     "session_id":"...","duration_ms":N,"is_error":bool,"usage":{...}}
            assistant_text_parts: List[str] = []
            reported_duration_ms: Optional[int] = None
            is_error = False

            for message in messages:
                msg_type = message.get("type", "")

                if msg_type == "system" and message.get("subtype") == "init":
                    sid = message.get("session_id")
                    if sid:
                        cursor_session_id = sid

                elif msg_type == "result":
                    result_content = str(message.get("result") or "")
                    sid = message.get("session_id")
                    if sid:
                        cursor_session_id = sid
                    if isinstance(message.get("duration_ms"), (int, float)):
                        reported_duration_ms = int(message["duration_ms"])
                    is_error = bool(message.get("is_error", False))
                    # cursor-agent does not currently report dollar cost;
                    # leave at 0.0 (token usage is in message["usage"] if needed).

                elif msg_type == "tool_call" and message.get("subtype") == "started":
                    tool_call_obj = message.get("tool_call") or {}
                    tool_name = _extract_cursor_tool_name(tool_call_obj)
                    # Args are nested one level deeper under the tool key
                    args: Dict[str, Any] = {}
                    for v in tool_call_obj.values():
                        if isinstance(v, dict) and isinstance(v.get("args"), dict):
                            args = v["args"]
                            break
                    tools_used.append(
                        {
                            "name": tool_name,
                            "timestamp": asyncio.get_event_loop().time(),
                            "input": args,
                        }
                    )

                elif msg_type == "assistant":
                    text = _extract_cursor_message_text(message)
                    if text:
                        assistant_text_parts.append(text)

            # Calculate duration (prefer cursor-agent's reported value)
            duration_ms = (
                reported_duration_ms
                if reported_duration_ms is not None
                else int((asyncio.get_event_loop().time() - start_time) * 1000)
            )

            # Use Cursor's session_id if available, otherwise fall back
            final_session_id = cursor_session_id or session_id or ""

            # Prefer the explicit result; fall back to concatenated assistant text
            if result_content:
                content = result_content.strip()
            else:
                content = "\n".join(assistant_text_parts).strip()

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
                num_turns=len(
                    [m for m in messages if m.get("type") in ("user", "assistant")]
                ),
                is_error=is_error,
                tools_used=tools_used,
                interrupted=interrupted,
            )

        except asyncio.TimeoutError:
            logger.error(
                "Cursor Agent command timed out",
                timeout_seconds=self.config.cursor_timeout_seconds,
            )
            raise ClaudeTimeoutError(
                f"Cursor Agent timed out after {self.config.cursor_timeout_seconds}s"
            )

        except Exception as e:
            logger.error(
                "Cursor Agent error", error=str(e), error_type=type(e).__name__
            )
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
        """Convert a cursor-agent stream-json event into a StreamUpdate.

        Event shapes (verified against ``cursor-agent --output-format stream-json``):
          - system/init:  {"type":"system","subtype":"init","session_id":"...",
                           "model":"...","cwd":"..."}
          - user:         {"type":"user","message":{"content":[{"type":"text",
                           "text":"..."}]}, "session_id":"..."}
          - assistant:    {"type":"assistant","message":{"content":[{"type":"text",
                           "text":"..."}]}, "session_id":"..."}
          - tool_call:    {"type":"tool_call","subtype":"started"|"completed",
                           "call_id":"...","tool_call":{"<name>ToolCall":{
                           "args":{...},"result":{...}?}}, ...}
          - result:       {"type":"result","subtype":"success","result":"...",
                           "session_id":"...","duration_ms":N,"is_error":bool,
                           "usage":{...}}
        """
        try:
            event_type = event.get("type", "")

            if event_type == "system" and event.get("subtype") == "init":
                update = StreamUpdate(
                    type="system",
                    metadata={
                        "session_id": event.get("session_id"),
                        "model": event.get("model"),
                        "cwd": event.get("cwd"),
                    },
                )
                await stream_callback(update)

            elif event_type == "assistant":
                text = _extract_cursor_message_text(event)
                if text:
                    await stream_callback(StreamUpdate(type="assistant", content=text))

            elif event_type == "user":
                text = _extract_cursor_message_text(event)
                if text:
                    await stream_callback(StreamUpdate(type="user", content=text))

            elif event_type == "tool_call":
                subtype = event.get("subtype", "")
                tool_call_obj = event.get("tool_call") or {}
                tool_name = _extract_cursor_tool_name(tool_call_obj)

                # Args and (on completion) result are nested under the tool key
                args: Dict[str, Any] = {}
                tool_result: Any = None
                for v in tool_call_obj.values():
                    if isinstance(v, dict):
                        if isinstance(v.get("args"), dict):
                            args = v["args"]
                        if "result" in v:
                            tool_result = v["result"]
                        break

                if subtype == "started":
                    await stream_callback(
                        StreamUpdate(
                            type="assistant",
                            tool_calls=[
                                {
                                    "name": tool_name,
                                    "input": args,
                                    "id": event.get("call_id", ""),
                                }
                            ],
                        )
                    )
                elif subtype == "completed":
                    await stream_callback(
                        StreamUpdate(
                            type="tool_result",
                            metadata={
                                "tool_name": tool_name,
                                "tool_output": tool_result,
                                "call_id": event.get("call_id"),
                            },
                        )
                    )

            elif event_type == "result":
                await stream_callback(
                    StreamUpdate(
                        type="result",
                        content=str(event.get("result") or ""),
                        metadata={
                            "session_id": event.get("session_id"),
                            "is_error": bool(event.get("is_error", False)),
                            "duration_ms": event.get("duration_ms"),
                            "usage": event.get("usage"),
                        },
                    )
                )

            elif event_type == "error":
                error_msg = (
                    event.get("message") or event.get("error") or "Unknown error"
                )
                await stream_callback(
                    StreamUpdate(
                        type="error",
                        content=str(error_msg),
                        metadata={"is_error": True},
                    )
                )

        except Exception as e:
            logger.warning(
                "Failed to handle Cursor stream event",
                error=str(e),
                event_type=event.get("type"),
            )
            # Don't re-raise to avoid breaking the stream
