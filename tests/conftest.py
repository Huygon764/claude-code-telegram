"""Pytest configuration and fixtures."""

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_env_file(monkeypatch):
    """Isolate every test from the developer's local .env file.

    Two leak paths to plug:
    1. ``Settings(...)`` reads ``.env`` via ``model_config['env_file']`` —
       patched to ``None`` here.
    2. ``load_config()`` calls ``dotenv.load_dotenv()``, which writes the
       file's keys into ``os.environ`` globally and would poison every
       subsequent test in the session — snapshot/restore os.environ
       around each test to undo that.

    Without this, tests that happen to land after one calling
    ``load_config()`` inherit values like ENABLE_PROJECT_THREADS and fail
    only on machines where the bot is actually configured.
    """
    from src.config.settings import Settings

    patched = dict(Settings.model_config)
    patched["env_file"] = None
    monkeypatch.setattr(Settings, "model_config", patched)

    saved_env = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved_env)


@pytest.fixture
def sample_user_id():
    """Sample Telegram user ID for testing."""
    return 123456789


@pytest.fixture
def sample_config():
    """Sample configuration for testing."""
    return {
        "telegram_bot_token": "test_token",
        "telegram_bot_username": "test_bot",
        "approved_directory": "/tmp/test_projects",
        "allowed_users": [123456789],
    }
