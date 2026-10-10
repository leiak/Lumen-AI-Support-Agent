import pytest


def test_settings_has_no_default_tenant_id_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Security P2 cleanup: default_tenant_id must not exist on Settings.

    A default tenant fallback is a footgun -- code paths that forget
    to pass tenant_id would silently read/write across tenants.

    Test runs `inspect.get_annotations` AND a runtime hasattr check so
    the assertion holds even if a future refactor renames, aliases, or
    otherwise re-introduces the field under another name.
    """
    import inspect

    from core.config import Settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "test-secret-32-chars-minimum-length")

    annotations = inspect.get_annotations(Settings)
    assert "default_tenant_id" not in annotations, (
        "default_tenant_id is no longer used (see tech-debt #16 + nitpick "
        "Security P2). Remove the field from core/config.py so future "
        "callers cannot silently fall back to a default tenant."
    )

    # Also pin the runtime: a Settings instance must not have the attr
    s = Settings(_env_file=None)
    assert not hasattr(s, "default_tenant_id"), (
        "Settings instance exposes a default_tenant_id attribute at runtime, "
        "even though no code path reads it. Delete the field to remove the "
        "footgun."
    )


def test_settings_ignores_default_tenant_id_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deprecated DEFAULT_TENANT_ID env var must no longer be honored.

    Historical behavior: Setting ``DEFAULT_TENANT_ID`` would populate the
    ``default_tenant_id`` field. After the cleanup, pydantic-settings must
    not warn-and-ignore on an unknown alias, so we assert it does not
    surface as an attribute either.
    """
    from core.config import Settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://x:y@localhost:5432/z")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "test-secret-32-chars-minimum-length")
    monkeypatch.setenv("DEFAULT_TENANT_ID", "tenant-abc")

    s = Settings(_env_file=None)
    assert not hasattr(s, "default_tenant_id"), (
        "DEFAULT_TENANT_ID env var should not populate any Settings field "
        "after the Security P2 cleanup."
    )
