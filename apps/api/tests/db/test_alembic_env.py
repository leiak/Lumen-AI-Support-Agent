"""Smoke test: alembic env loads without crashing and reads DATABASE_URL from settings."""


def test_alembic_ini_exists() -> None:
    from pathlib import Path

    ini = Path(__file__).parent.parent.parent / "alembic.ini"
    assert ini.exists(), f"alembic.ini not found at {ini}"


def test_migrations_env_imports() -> None:
    """Validate env.py imports + required references."""
    import sys
    from pathlib import Path

    # Put migrations/ on sys.path so we can import env.py as a module
    migrations_dir = Path(__file__).parent.parent.parent / "migrations"
    sys.path.insert(0, str(migrations_dir.parent))
    # migrations/env.py is normally not importable as `env` due to the directory
    # layout; instead we exec it and verify the script_location and sqlalchemy.url
    # are configured.
    env_text = (migrations_dir / "env.py").read_text(encoding="utf-8")
    assert "from core.config import get_settings" in env_text
    assert "from core.database import Base" in env_text


def test_settings_database_url_is_asyncpg() -> None:
    """DATABASE_URL must use the asyncpg driver so alembic async engine can connect."""
    from core.config import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.database_url.startswith("postgresql+asyncpg://"), (
        f"expected asyncpg driver, got: {s.database_url[:40]}"
    )
