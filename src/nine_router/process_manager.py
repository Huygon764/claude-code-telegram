"""Start/stop/status for a local 9Router Docker container."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urljoin

import httpx
import structlog

from src.config.settings import Settings

logger = structlog.get_logger()


class ContainerState(str, Enum):
    """Docker container presence/running state."""

    MISSING = "missing"
    STOPPED = "stopped"
    RUNNING = "running"


@dataclass(frozen=True)
class NineRouterStatus:
    """Combined Docker + HTTP health snapshot."""

    container: ContainerState
    healthy: bool
    detail: str


@dataclass(frozen=True)
class NineRouterActionResult:
    """Outcome of a start/stop action."""

    success: bool
    message: str
    status: NineRouterStatus


class NineRouterProcessManager:
    """Manage the local 9Router Docker container from the bot."""

    def __init__(self, config: Settings) -> None:
        self.config = config

    @property
    def health_url(self) -> str:
        base = self.config.nine_router_base_url.rstrip("/") + "/"
        return urljoin(base, "api/health")

    async def status(self) -> NineRouterStatus:
        """Return container state and HTTP health."""
        container = await self._container_state()
        healthy = False
        detail = "Container not running"

        if container == ContainerState.RUNNING:
            healthy = await self._check_health()
            detail = "Healthy" if healthy else "Container up but health check failed"

        elif container == ContainerState.STOPPED:
            detail = "Container stopped"
        else:
            detail = "Container not found"

        return NineRouterStatus(
            container=container,
            healthy=healthy,
            detail=detail,
        )

    async def start(self) -> NineRouterActionResult:
        """Ensure 9Router is running and healthy."""
        current = await self.status()
        if current.healthy:
            return NineRouterActionResult(
                success=True,
                message="9Router is already running.",
                status=current,
            )

        if current.container == ContainerState.RUNNING:
            ready = await self._wait_for_health()
            status = await self.status()
            if ready:
                return NineRouterActionResult(
                    success=True,
                    message="9Router is running.",
                    status=status,
                )
            return NineRouterActionResult(
                success=False,
                message="Container is up but /api/health did not become ready in time.",
                status=status,
            )

        if current.container == ContainerState.STOPPED:
            code, out, err = await self._docker(
                "start", self.config.nine_router_container_name
            )
            if code != 0:
                status = await self.status()
                return NineRouterActionResult(
                    success=False,
                    message=f"Failed to start container: {err or out}",
                    status=status,
                )
            ready = await self._wait_for_health()
            status = await self.status()
            if ready:
                return NineRouterActionResult(
                    success=True,
                    message="9Router started.",
                    status=status,
                )
            return NineRouterActionResult(
                success=False,
                message="Container started but health check timed out.",
                status=status,
            )

        return await self._create_container()

    async def stop(self) -> NineRouterActionResult:
        """Stop the 9Router container if it exists."""
        current = await self.status()
        if current.container == ContainerState.MISSING:
            return NineRouterActionResult(
                success=True,
                message="9Router container is not installed.",
                status=current,
            )
        if current.container == ContainerState.STOPPED:
            return NineRouterActionResult(
                success=True,
                message="9Router is already stopped.",
                status=current,
            )

        code, out, err = await self._docker(
            "stop", self.config.nine_router_container_name
        )
        status = await self.status()
        if code != 0:
            return NineRouterActionResult(
                success=False,
                message=f"Failed to stop container: {err or out}",
                status=status,
            )
        return NineRouterActionResult(
            success=True,
            message="9Router stopped.",
            status=status,
        )

    async def _create_container(self) -> NineRouterActionResult:
        repo = self.config.nine_router_repo_path
        if not repo:
            status = await self.status()
            return NineRouterActionResult(
                success=False,
                message=(
                    "9Router container not found. Set NINE_ROUTER_REPO_PATH in .env "
                    "so the bot can run the install script."
                ),
                status=status,
            )

        repo_path = Path(repo).expanduser().resolve()
        script = repo_path / self.config.nine_router_start_script
        if not script.is_file():
            status = await self.status()
            return NineRouterActionResult(
                success=False,
                message=f"Start script not found: {script}",
                status=status,
            )

        logger.info(
            "Creating 9Router container via start script",
            script=str(script),
        )
        code, out, err = await self._run_shell(
            f"bash {script.name}",
            cwd=repo_path,
            timeout=self.config.nine_router_start_timeout_seconds,
        )
        status = await self.status()
        if code != 0:
            tail = (err or out).strip().splitlines()
            snippet = tail[-1] if tail else "unknown error"
            return NineRouterActionResult(
                success=False,
                message=f"Start script failed: {snippet}",
                status=status,
            )

        ready = await self._wait_for_health()
        status = await self.status()
        if ready:
            return NineRouterActionResult(
                success=True,
                message="9Router installed and running.",
                status=status,
            )
        return NineRouterActionResult(
            success=False,
            message="Container created but health check timed out.",
            status=status,
        )

    async def _container_state(self) -> ContainerState:
        code, out, _ = await self._docker(
            "inspect",
            "-f",
            "{{.State.Running}}",
            self.config.nine_router_container_name,
        )
        if code != 0:
            return ContainerState.MISSING
        if out.strip().lower() == "true":
            return ContainerState.RUNNING
        return ContainerState.STOPPED

    async def _check_health(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(self.health_url)
                if response.status_code != 200:
                    return False
                payload = response.json()
                return bool(payload.get("ok"))
        except (httpx.HTTPError, ValueError):
            return False

    async def _wait_for_health(self) -> bool:
        deadline = time.monotonic() + self.config.nine_router_health_wait_seconds
        while time.monotonic() < deadline:
            if await self._check_health():
                return True
            await asyncio.sleep(2)
        return False

    async def _docker(self, *args: str, timeout: float = 30.0) -> tuple[int, str, str]:
        return await self._run_cmd("docker", *args, timeout=timeout)

    async def _run_shell(
        self, command: str, *, cwd: Path, timeout: float
    ) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return (124, "", "command timed out")
        return (
            proc.returncode if proc.returncode is not None else -1,
            out_b.decode("utf-8", "replace").strip(),
            err_b.decode("utf-8", "replace").strip(),
        )

    async def _run_cmd(
        self, program: str, *args: str, timeout: float = 30.0
    ) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                program,
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return (127, "", f"{program} not available: {exc}")
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return (124, "", f"{program} command timed out")
        return (
            proc.returncode if proc.returncode is not None else -1,
            out_b.decode("utf-8", "replace").strip(),
            err_b.decode("utf-8", "replace").strip(),
        )
