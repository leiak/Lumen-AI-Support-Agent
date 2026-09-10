import pytest

from core.config import Settings


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "test-secret-32-chars-minimum-length")

    s = Settings()
    assert s.database_url == "postgresql+asyncpg://x:y@localhost:5432/z"
    assert s.jwt_secret == "test-secret-32-chars-minimum-length"  # noqa: S105
    assert s.environment == "development"


def test_settings_rejects_short_jwt_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "short")

    with pytest.raises(ValueError, match="JWT_SECRET must be at least 32"):
        Settings()