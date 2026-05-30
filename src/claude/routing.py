"""Anthropic API routing for direct vs 9Router proxy."""

import os
from contextlib import contextmanager
from typing import Iterator, Literal, Mapping, Optional

from src.config.settings import Settings

AnthropicRouting = Literal["direct", "nine_router"]

_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_API_KEY",
)


@contextmanager
def anthropic_routing_env(
    config: Settings, routing: AnthropicRouting
) -> Iterator[None]:
    """Temporarily configure Anthropic env vars for the chosen routing profile."""
    saved: dict[str, Optional[str]] = {key: os.environ.get(key) for key in _ENV_KEYS}

    try:
        if routing == "nine_router":
            if not config.nine_router_enabled:
                raise RuntimeError(
                    "9Router is not configured. Set NINE_ROUTER_AUTH_TOKEN in .env."
                )
            os.environ["ANTHROPIC_BASE_URL"] = config.nine_router_base_url
            os.environ["ANTHROPIC_AUTH_TOKEN"] = config.nine_router_auth_token_str or ""
            if config.nine_router_model:
                os.environ["ANTHROPIC_MODEL"] = config.nine_router_model
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"):
                os.environ.pop(key, None)
            if config.nine_router_model and os.environ.get("ANTHROPIC_MODEL") == (
                config.nine_router_model
            ):
                os.environ.pop("ANTHROPIC_MODEL", None)
            if config.anthropic_api_key_str:
                os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key_str
            elif "ANTHROPIC_API_KEY" not in saved:
                os.environ.pop("ANTHROPIC_API_KEY", None)

        yield
    finally:
        _restore_env(saved)


def _restore_env(saved: Mapping[str, Optional[str]]) -> None:
    for key in _ENV_KEYS:
        value = saved.get(key)
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
