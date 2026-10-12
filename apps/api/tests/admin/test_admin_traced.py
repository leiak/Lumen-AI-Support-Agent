"""M6.4 — verify ``@traced`` on admin mutations produces spans with
the right names.

Three PUBLIC mutation methods on
:class:`admin.repository.AdminTenantLLMConfigRepository` are
decorated in M6.4:

* ``upsert``  → ``admin.llm_config.upsert``
* ``update``  → ``admin.llm_config.update``
* ``delete``  → ``admin.llm_config.delete``  (note: the M6.4 plan
  referenced ``delete_by_tenant_provider`` which is the inner-
  repository surface; the admin wrapper exposes the same intent
  under the name ``delete``).

These tests cover a SINGLE mutation per test — they exercise the
wrapper, not the inner repo, so each call should land exactly one
top-level ``admin.llm_config.*`` span.

The conftest at ``apps/api/tests/observability/conftest.py``
install the in-memory span exporter at module-load time;
``tests/admin/conftest.py`` re-exports the ``memory_exporter``
fixture so the spans produced by the wrapper land in the buffer.
"""
from __future__ import annotations

from typing import Any

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import delete as sql_delete

from admin.repository import AdminTenantLLMConfigRepository
from admin.schemas.tenant_llm_config import (
    TenantLLMConfigCreate,
    TenantLLMConfigUpdate,
)
from core.config import reset_settings
from tenant.enums import TenantPlan
from tenant.models import Tenant
from tenant.repository import TenantRepository


# ---------------------------------------------------------------------------
# Per-test scaffolding — fresh tenant + Fernet key + repo
# ---------------------------------------------------------------------------


@pytest.fixture
async def sample_tenant_with_fernet(monkeypatch: pytest.MonkeyPatch) -> str:
    """Yield a tenant ULID after seeding Fernet + a Tenant row.

    Each test seeds its own Fernet key so the cipher inside
    :class:`AdminTenantLLMConfigRepository` decrypts correctly.
    """
    monkeypatch.setenv("TENANT_LLM_FERNET_KEY", Fernet.generate_key().decode())
    reset_settings()

    tenant = await TenantRepository().create(
        name="M6.4 Admin Span Test Tenant",
        plan=TenantPlan.PRO,
    )
    return tenant.id


async def _delete_tenant(tenant_id: str) -> None:
    from core.database import get_session

    from llm_client.tenant_config_models import TenantLLMConfig

    async with get_session() as session:
        await session.execute(
            sql_delete(TenantLLMConfig).where(
                TenantLLMConfig.tenant_id == tenant_id
            )
        )
        await session.commit()
        t = await session.get(Tenant, tenant_id)
        if t is not None:
            await session.delete(t)
            await session.commit()


@pytest.fixture
async def repo_under_test(sample_tenant_with_fernet: str):
    """Yield ``(repo, tenant_id)`` — repository + tenant ULID, with teardown."""
    tenant_id = sample_tenant_with_fernet
    repo = AdminTenantLLMConfigRepository()
    try:
        yield repo, tenant_id
    finally:
        await _delete_tenant(tenant_id)


# ---------------------------------------------------------------------------
# Step 4.7 — one span per mutation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_emits_span(
    memory_exporter: Any,
    repo_under_test: Any,
) -> None:
    """``upsert(...)`` → one ``admin.llm_config.upsert`` span."""
    repo, tenant_id = repo_under_test
    payload = TenantLLMConfigCreate(
        provider_name="minimax",
        api_key="sk-test-otlp",
        base_url=None,
        model=None,
        enabled=True,
    )

    await repo.upsert(
        tenant_id=tenant_id,
        payload=payload,
        changed_by="admin-1",
        api_key_sha256="otlp-fake-sha256",
    )

    spans = memory_exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "admin.llm_config.upsert" in names, (
        f"expected admin.llm_config.upsert span, got names={sorted(names)!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_update_emits_span(
    memory_exporter: Any,
    repo_under_test: Any,
) -> None:
    """``update(...)`` → one ``admin.llm_config.update`` span."""
    repo, tenant_id = repo_under_test
    # Seed a row first so the update has something to patch.
    await repo.upsert(
        tenant_id=tenant_id,
        payload=TenantLLMConfigCreate(
            provider_name="anthropic",
            api_key="sk-anthropic-seed",
            base_url=None,
            model=None,
            enabled=True,
        ),
        changed_by="admin-seed",
        api_key_sha256="seed-sha256",
    )
    # Clear the exporter seed (the upsert span above shouldn't
    # contaminate the assertion for the update call).
    memory_exporter.clear()

    await repo.update(
        tenant_id=tenant_id,
        provider_name="anthropic",
        payload=TenantLLMConfigUpdate(enabled=False),
        changed_by="admin-2",
    )

    spans = memory_exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "admin.llm_config.update" in names, (
        f"expected admin.llm_config.update span, got names={sorted(names)!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_delete_emits_span(
    memory_exporter: Any,
    repo_under_test: Any,
) -> None:
    """``delete(...)`` → one ``admin.llm_config.delete`` span."""
    repo, tenant_id = repo_under_test
    # Seed a row so the delete has something to remove.
    await repo.upsert(
        tenant_id=tenant_id,
        payload=TenantLLMConfigCreate(
            provider_name="openai",
            api_key="sk-openai-seed",
            base_url=None,
            model=None,
            enabled=True,
        ),
        changed_by="admin-seed",
        api_key_sha256="openai-sha256",
    )
    memory_exporter.clear()

    deleted = await repo.delete(
        tenant_id=tenant_id,
        provider_name="openai",
        changed_by="admin-3",
    )
    assert deleted is True

    spans = memory_exporter.get_finished_spans()
    names = {s.name for s in spans}
    assert "admin.llm_config.delete" in names, (
        f"expected admin.llm_config.delete span, got names={sorted(names)!r}"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_admin_spans_do_not_leak_pii_attributes(
    memory_exporter: Any,
    repo_under_test: Any,
) -> None:
    """The admin spans carry NO PII attributes.

    ``@traced`` only filters static ``attributes`` dicts; with no
    static dict on these wrappers the spans have only the default
    ``code.function`` / ``code.namespace`` attributes that OTel
    adds automatically. There must be NO message / customer /
    api_key / secret / token key in the span payload.
    """
    repo, tenant_id = repo_under_test
    memory_exporter.clear()
    await repo.upsert(
        tenant_id=tenant_id,
        payload=TenantLLMConfigCreate(
            provider_name="minimax",
            api_key="sk-pii-attrs",
            base_url=None,
            model=None,
            enabled=True,
        ),
        changed_by="admin-pii",
        api_key_sha256="pii-sha256",
    )

    spans = memory_exporter.get_finished_spans()
    admin_spans = [s for s in spans if s.name.startswith("admin.llm_config.")]
    assert admin_spans, "expected at least one admin.llm_config.* span"
    forbidden_tokens = (
        "api_key",
        "message",
        "customer",
        "password",
        "secret",
        "token",
    )
    for span in admin_spans:
        attr_keys = " ".join((span.attributes or {}).keys())
        for token in forbidden_tokens:
            assert token not in attr_keys, (
                f"PII token {token!r} found in span {span.name!r} "
                f"attributes={attr_keys!r}"
            )
