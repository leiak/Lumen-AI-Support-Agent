"""Integration tests for per-tenant model override wiring (M5.2 Task 3).

Verifies the wrap in :meth:`TenantLLMConfigCache._raise_or_return`:
when any enabled ``tenant_llm_configs`` row has a non-null ``model``
column, the gateway resolver is wrapped with
:class:`TenantModelOverrideResolver` so requests are rewritten to the
tenant's per-provider model override before reaching the provider.

Mirrors the autouse + Fernet-key + tenant helper pattern from
``test_tenant_resolver_with_chain.py`` (M5.1 Task 2) and
``test_tenant_fallback_chain_models.py`` (M4.B). Each test gets a
fresh event loop, a fresh Fernet key, and a fresh
``TenantLLMConfigCache`` instance so cache rows never bleed.

Coverage:
  1. ``tenant_llm_configs.model=None`` → no wrap (back-compat).
  2. ``tenant_llm_configs.model`` set → wrapper installed with the
     matching provider→model dict.
  3. Multi-provider: only enabled rows with non-null ``model`` land
     in the override dict.
  4. Disabled rows are excluded from the override dict.
  5. Per-tenant cache entries don't bleed across tenants.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy import delete

from core.database import get_sessionmaker
from core.id_gen import new_id
from llm_client.tenant_config_models import TenantLLMConfigRepository
from llm_client.tenant_model_override_resolver import (
    TenantModelOverrideResolver,
)
from llm_client.tenant_resolver import (
    TenantLLMConfigCache,
    build_tenant_resolver,
)
from tenant.models import Tenant
from tenant.repository import TenantRepository

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_singletons() -> None:
    """Reset settings + engine + sessionmaker between tests.

    Each pytest-asyncio test gets a fresh event loop. Without resets
    the SQLAlchemy engine from a previous test would be reused on a
    closed loop, raising 'Event loop is closed'.
    """
    from core.config import reset_settings
    from core.database import reset_engine, reset_sessionmaker

    reset_settings()
    reset_engine()
    reset_sessionmaker()
    yield
    reset_settings()
    reset_engine()
    reset_sessionmaker()


@pytest.fixture(autouse=True)
def _ensure_fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Generate a fresh Fernet key per test (M4.C tenant BYOK encryption).

    The cache's ``_build_providers`` calls ``self._cipher.decrypt(...)``
    which raises if the master key is missing. We pin a fresh key here
    so the cipher created inside the test fixture matches the
    ciphertexts stored on the rows.
    """
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", Fernet.generate_key().decode())
    from core.config import reset_settings

    reset_settings()


@pytest_asyncio.fixture
async def tenant_with_optional_model():
    """Yield a callable that creates a tenant + ONE LLM config row.

    The callable takes ``model: str | None = None`` and returns the
    ``(tenant, row)`` tuple. Cleanup is cascade-driven (deleting the
    tenant cascades to ``tenant_llm_configs`` via FK).
    """
    from core.config import get_settings
    from llm_client.tenant_config_crypto import TenantLLMConfigCipher
    from tenant.enums import TenantPlan

    cipher = TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)
    tenant = await TenantRepository().create(
        name=f"Model override tenant {new_id()[:8]}",
        plan=TenantPlan.PRO,
    )

    async def _setup(model: str | None = None):
        row = await TenantLLMConfigRepository().upsert(
            tenant_id=tenant.id,
            provider_name="anthropic",
            encrypted_api_key=cipher.encrypt("sk-test-placeholder"),
            base_url=None,
            enabled=True,
            model=model,
        )
        return tenant, row

    try:
        yield _setup
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


@pytest_asyncio.fixture
async def fresh_cache() -> TenantLLMConfigCache:
    """Per-test cache with a tiny TTL — prevents stale rows leaking.

    The cache needs both a Fernet cipher (for ``_build_providers``) and
    a settings mock (for the model defaults threaded into each
    provider). We pull the cipher from the freshly-set Fernet key in
    the env so the per-test fixture can encrypt/decrypt consistently.
    """
    from unittest.mock import MagicMock

    from core.config import get_settings
    from llm_client.tenant_config_crypto import TenantLLMConfigCipher

    cipher = TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)
    settings = MagicMock()
    settings.llm_fallback_chain = None
    settings.llm_tenant_fallback_chain_required = False
    settings.llm_fallback_attempt_timeout_s = None
    settings.llm_tenant_fallback_chain_ttl_s = 60.0
    settings.llm_tenant_fallback_chain_cache_maxsize = 1024
    settings.default_llm_model = "claude-3-5-sonnet-20241022"
    settings.minimax_model = None
    settings.minimax_base_url = None
    settings.openai_model = None
    return TenantLLMConfigCache(
        ttl_s=1.0,
        maxsize=10,
        cipher=cipher,
        settings=settings,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_wrap_when_model_is_null(
    tenant_with_optional_model,
    fresh_cache: TenantLLMConfigCache,
) -> None:
    """model=None → resolver is NOT wrapped with TenantModelOverrideResolver.

    Pre-M5.2 behavior must be preserved when no row has a model override.
    """
    tenant, _row = await tenant_with_optional_model(model=None)
    resolver = await build_tenant_resolver(tenant.id, cache=fresh_cache)

    assert not isinstance(resolver, TenantModelOverrideResolver), (
        "expected plain gateway resolver when no model override is set, "
        f"got {type(resolver).__name__}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrap_when_model_is_set(
    tenant_with_optional_model,
    fresh_cache: TenantLLMConfigCache,
) -> None:
    """model set → resolver IS wrapped; overrides dict has the override."""
    tenant, _row = await tenant_with_optional_model(model="claude-haiku-4-5")
    resolver = await build_tenant_resolver(tenant.id, cache=fresh_cache)

    assert isinstance(resolver, TenantModelOverrideResolver), (
        "expected TenantModelOverrideResolver when model is set, "
        f"got {type(resolver).__name__}"
    )
    assert resolver._overrides == {"anthropic": "claude-haiku-4-5"}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrap_with_multiple_providers_partial_overrides(
    fresh_cache: TenantLLMConfigCache,
) -> None:
    """Two rows: anthropic WITH model + openai WITHOUT → only anthropic
    lands in the override dict (openai was enabled but has model=None).
    """
    from core.config import get_settings
    from llm_client.tenant_config_crypto import TenantLLMConfigCipher
    from tenant.enums import TenantPlan

    cipher = TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)
    tenant = await TenantRepository().create(
        name=f"Multi-provider model override {new_id()[:8]}",
        plan=TenantPlan.PRO,
    )
    try:
        await TenantLLMConfigRepository().upsert(
            tenant_id=tenant.id,
            provider_name="anthropic",
            encrypted_api_key=cipher.encrypt("sk-anthropic"),
            base_url=None,
            enabled=True,
            model="claude-haiku-4-5",
        )
        await TenantLLMConfigRepository().upsert(
            tenant_id=tenant.id,
            provider_name="openai",
            encrypted_api_key=cipher.encrypt("sk-openai"),
            base_url=None,
            enabled=True,
            model=None,  # explicit None → no override for openai
        )

        resolver = await build_tenant_resolver(tenant.id, cache=fresh_cache)

        assert isinstance(resolver, TenantModelOverrideResolver)
        assert resolver._overrides == {"anthropic": "claude-haiku-4-5"}, (
            "openai should NOT appear in overrides because its row.model is None"
        )
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_disabled_rows_excluded_from_overrides(
    fresh_cache: TenantLLMConfigCache,
) -> None:
    """A disabled row with ``model`` set must NOT contribute to the
    override dict.

    We seed TWO rows: an enabled openai row (no model override) and a
    disabled anthropic row (with model override). The DB query
    ``list_by_tenant(enabled_only=True)`` already filters out the
    disabled row, but this test still verifies the in-method
    ``if row.enabled and row.model is not None`` filter is the last
    line of defence — it must not let a disabled row's model leak into
    the override dict, so the wrapper is NOT installed.
    """
    from core.config import get_settings
    from llm_client.tenant_config_crypto import TenantLLMConfigCipher
    from tenant.enums import TenantPlan

    cipher = TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)
    tenant = await TenantRepository().create(
        name=f"Disabled-row model override {new_id()[:8]}",
        plan=TenantPlan.PRO,
    )
    try:
        # Enabled openai row — no model override; this is the only
        # row ``list_by_tenant(enabled_only=True)`` will return.
        await TenantLLMConfigRepository().upsert(
            tenant_id=tenant.id,
            provider_name="openai",
            encrypted_api_key=cipher.encrypt("sk-openai"),
            base_url=None,
            enabled=True,
            model=None,
        )
        # Disabled anthropic row — has a model override that MUST be
        # ignored by the wrap filter.
        await TenantLLMConfigRepository().upsert(
            tenant_id=tenant.id,
            provider_name="anthropic",
            encrypted_api_key=cipher.encrypt("sk-anthropic"),
            base_url=None,
            enabled=False,
            model="claude-haiku-4-5",
        )

        resolver = await build_tenant_resolver(tenant.id, cache=fresh_cache)
        # The only enabled row has model=None, so overrides is empty
        # → no wrap. If the disabled row leaked into the dict, the
        # wrapper would be installed with anthropic→claude-haiku-4-5.
        assert not isinstance(resolver, TenantModelOverrideResolver), (
            "disabled row must not contribute to overrides dict; "
            f"got {type(resolver).__name__}"
        )
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant.id))
            await session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_per_tenant_overrides_isolated(
    fresh_cache: TenantLLMConfigCache,
) -> None:
    """Two tenants with different model overrides → each gets its own
    wrapper with the correct dict. Per-tenant cache entries don't bleed.
    """
    from core.config import get_settings
    from llm_client.tenant_config_crypto import TenantLLMConfigCipher
    from tenant.enums import TenantPlan

    cipher = TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)
    tenant_a = await TenantRepository().create(
        name=f"Model override A {new_id()[:8]}",
        plan=TenantPlan.PRO,
    )
    tenant_b = await TenantRepository().create(
        name=f"Model override B {new_id()[:8]}",
        plan=TenantPlan.PRO,
    )
    try:
        await TenantLLMConfigRepository().upsert(
            tenant_id=tenant_a.id,
            provider_name="anthropic",
            encrypted_api_key=cipher.encrypt("sk-a"),
            base_url=None,
            enabled=True,
            model="claude-haiku-4-5",
        )
        await TenantLLMConfigRepository().upsert(
            tenant_id=tenant_b.id,
            provider_name="anthropic",
            encrypted_api_key=cipher.encrypt("sk-b"),
            base_url=None,
            enabled=True,
            model="claude-sonnet-4-5",
        )

        resolver_a = await build_tenant_resolver(tenant_a.id, cache=fresh_cache)
        resolver_b = await build_tenant_resolver(tenant_b.id, cache=fresh_cache)

        assert isinstance(resolver_a, TenantModelOverrideResolver)
        assert isinstance(resolver_b, TenantModelOverrideResolver)
        assert resolver_a._overrides == {"anthropic": "claude-haiku-4-5"}
        assert resolver_b._overrides == {"anthropic": "claude-sonnet-4-5"}
        # Sanity: the two resolvers are distinct objects (no aliasing).
        assert resolver_a is not resolver_b
    finally:
        sm = get_sessionmaker()
        async with sm() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant_a.id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_b.id))
            await session.commit()
