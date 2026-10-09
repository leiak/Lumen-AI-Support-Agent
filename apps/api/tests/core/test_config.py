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


def test_settings_rejects_known_dev_secret_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production environment must not use any known dev/test secret sentinel."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "dev-secret-please-change-in-production-32chars")
    monkeypatch.setenv("ENVIRONMENT", "production")

    with pytest.raises(ValueError, match=r"placeholder|dev.?secret|example"):
        Settings()


def test_settings_uses_defaults_for_optional_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional fields with defaults are populated when env vars are not set."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "test-secret-32-chars-minimum-length")
    # Clear optional env vars that may leak from the developer's shell
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OLLAMA_BASE_URL", "QDRANT_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    # _env_file=None bypasses .env reading for test isolation
    s = Settings(_env_file=None)
    assert s.qdrant_url == "http://localhost:6333"
    assert s.qdrant_api_key is None
    assert s.jwt_algorithm == "HS256"
    assert s.jwt_access_token_ttl_minutes == 60
    assert s.default_llm_model == "claude-3-5-sonnet-20241022"
    assert s.database_pool_size == 10
    assert s.anthropic_api_key is None
    assert s.ollama_base_url is None


def test_settings_rejects_change_me_jwt_secret_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The .env.example default (change-me-...) must be rejected in production."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "change-me-to-32-char-random-string")
    monkeypatch.setenv("ENVIRONMENT", "production")

    with pytest.raises(ValueError, match=r"placeholder|change.?me|dev.?secret|example"):
        Settings()


def test_settings_rejects_low_entropy_jwt_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """32+ char secret with low unique-character count must be rejected."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    # 40 chars, only 1 unique character
    monkeypatch.setenv("JWT_SECRET", "a" * 40)
    monkeypatch.setenv("ENVIRONMENT", "production")

    with pytest.raises(ValueError, match=r"entropy|unique|placeholder"):
        Settings()


def test_settings_accepts_high_entropy_jwt_secret_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 32+ char secret with diverse characters is accepted even in production."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv(
        "JWT_SECRET", "xK3mF9pL2qR8tN5vW7yA1bC4dE6gH0iJ"
    )
    monkeypatch.setenv("ENVIRONMENT", "production")

    s = Settings()
    assert s.jwt_secret == "xK3mF9pL2qR8tN5vW7yA1bC4dE6gH0iJ"  # noqa: S105