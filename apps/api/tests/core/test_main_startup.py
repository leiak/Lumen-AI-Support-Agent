import logging

import pytest

from core.config import get_settings, reset_settings


def test_cors_localhost_only_production_logs_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Production deployment with localhost-only CORS origins MUST log
    a WARNING at startup."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "xK3mF9pL2qR8tN5vW7yA1bC4dE6gH0iJ")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv(
        "WIDGET_ALLOWED_ORIGINS_GLOBAL", "http://localhost:5173,http://localhost:3000"
    )
    reset_settings()

    from main import _cors_startup_warnings

    settings = get_settings()
    with caplog.at_level(logging.WARNING):
        _cors_startup_warnings(settings)

    assert any("CORS" in rec.message and "localhost" in rec.message for rec in caplog.records)