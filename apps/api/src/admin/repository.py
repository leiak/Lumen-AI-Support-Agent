"""Admin-facing repository layer.

M4.C Task 4 — admin BYOK tenant-config repository.

The admin layer adds two layers of safety on top of the low-level
:class:`llm_client.tenant_config_models.TenantLLMConfigRepository`:

1. **Tenant existence check** — refuse to upsert / list configs for a
   tenant that doesn't exist in the ``tenants`` table. Without this
   a malicious or buggy admin client could spam rows tied to a
   non-existent tenant_id; the FK constraint would catch it on
   INSERT but on upsert the ON CONFLICT branch would silently
   succeed against a stale row. Better to 404 early.
2. **Encryption boundary** — wraps the
   :class:`TenantLLMConfigCipher` so callers (the API layer) never
   touch plaintext / ciphertext bytes directly. The repository is
   the ONLY layer that holds the cipher key and the ONLY one that
   ever produces ciphertext for storage.

The admin repository never returns the decrypted key; that contract
is enforced by :class:`admin.schemas.tenant_llm_config.TenantLLMConfigRead`,
which carries no key fields at all.

M4.D Task 4 — admin budget config repository.

:class:`AdminTenantBudgetRepository` mirrors the same tenant-existence
check pattern. Budget config carries no secrets, so there is no
encryption layer — just the existence check + delegation to the inner
:class:`TenantBudgetRepository`.

M5.2 — added ``model`` column handling on POST/PATCH. PATCH uses
``payload.model_fields_set`` to distinguish "field omitted entirely"
(no-op) from "field sent as null or empty string" (explicit clear),
so the inner repo's ``_UNSET`` sentinel contract still holds.
"""
from __future__ import annotations

from admin.schemas.budget import TenantBudgetCreate
from admin.schemas.fallback_chain import TenantFallbackChainUpsert
from admin.schemas.tenant_llm_config import TenantLLMConfigCreate, TenantLLMConfigUpdate
from budget.models import TenantBudget
from budget.repository import TenantBudgetRepository
from core.config import get_settings
from llm_client.tenant_config_crypto import TenantLLMConfigCipher
from llm_client.tenant_config_models import (
    _UNSET,
    TenantLLMConfig,
    TenantLLMConfigRepository,
)
from llm_client.tenant_fallback_chain_models import (
    TenantFallbackChain,
    TenantFallbackChainRepository,
)
from tenant.repository import TenantRepository


def _diff_upsert(
    *,
    old: TenantLLMConfig | None,
    payload_model_fields_set: set[str] | None,
    new_base_url: str | None,
    new_model: str | None,
    new_enabled: bool | None,
    new_api_key_sha256: str | None,
    old_api_key_sha256: str | None = None,
) -> list[tuple[str, str | None, str | None]]:
    """Produce list of (field, old_repr, new_repr) audit tuples.

    Behavior (M5.3 spec §6.5):
    - POST (insert): ``payload_model_fields_set=None`` → diff all
      fields. ``old is None``, so every field's old_repr is None
      (or the literal "sha256:initial" for api_key).
    - PATCH: ``payload_model_fields_set`` is the set of fields in
      the payload. Each field is included in the diff ONLY if its
      new value differs from the DB row's old value.
    - DELETE is not handled here; wrapper writes ('__deleted__',
      provider, None) directly.

    ``new_api_key_sha256`` is required on POST (passed by the route
    layer after computing ``hashlib.sha256(plaintext).hexdigest()``)
    and is None on PATCH (PATCH never rotates the key).

    ``old_api_key_sha256`` (M5.3 — Task 2 implementation note) is the
    SHA-256 fingerprint of the previously stored plaintext, fetched
    by the wrapper from the most recent prior audit row's
    ``new_value`` (for the same ``(tenant, provider, field=api_key)``).
    It is optional; when provided AND equal to ``new_api_key_sha256``
    the helper emits no api_key diff (no-op rotation = no audit
    row). When omitted, the helper emits unconditionally on POST.
    """
    diffs: list[tuple[str, str | None, str | None]] = []

    # --- api_key ---
    # POST always has a new SHA-256; PATCH never does. On insert, the
    # old_repr is the sentinel "sha256:initial" (not None — the spec
    # wants a first-class audit record of the initial key). On
    # update, the spec (§5.2) lets us record the prior hash when the
    # wrapper fetched it; if the new hash equals the prior hash the
    # operator didn't actually rotate and we skip the audit row
    # entirely (no churn).
    if new_api_key_sha256 is not None:
        if old is None:
            diffs.append(("api_key", "sha256:initial", new_api_key_sha256))
        elif (
            old_api_key_sha256 is not None
            and old_api_key_sha256 == new_api_key_sha256
        ):
            # No-op rotation — same plaintext → no diff.
            pass
        else:
            # Real rotation (or no prior hash available for
            # comparison). When ``old_api_key_sha256`` is None the
            # auditor can still recover the prior hash by reading
            # the most recent prior audit row's ``new_value`` for
            # the same (tenant, provider, field=api_key).
            diffs.append(
                ("api_key", old_api_key_sha256, new_api_key_sha256),
            )

    # --- base_url / model / enabled ---
    # On POST (fields_set=None) we always include these. On PATCH we
    # only include fields the operator actually sent.
    def _maybe(
        field_name: str,
        new_value: str | None,
        old_value: str | None,
    ) -> None:
        if payload_model_fields_set is not None and field_name not in payload_model_fields_set:
            return
        if old is None:
            # Insert — every non-key field is "new". The diff is
            # always written because the value is brand-new (None →
            # something).
            diffs.append((field_name, None, new_value))
        else:
            if new_value != old_value:
                diffs.append((field_name, old_value, new_value))

    _maybe("base_url", new_base_url, old.base_url if old else None)
    _maybe("model", new_model, old.model if old else None)
    _maybe(
        "enabled",
        str(new_enabled) if new_enabled is not None else None,
        str(old.enabled) if old else None,
    )
    return diffs


class AdminTenantLLMConfigRepository:
    """Admin-facing wrapper around :class:`TenantLLMConfigRepository`.

    Responsibilities:

    * Verify the tenant exists before any upsert / list (404 otherwise).
    * Encrypt the plaintext at the boundary; never leak it past this class.
    * Wrap every encrypt / upsert pair so the API endpoints only see
      :class:`TenantLLMConfig` rows — never the bytes.
    """

    def __init__(self) -> None:
        self._inner = TenantLLMConfigRepository()
        self._cipher = TenantLLMConfigCipher(get_settings().tenant_llm_fernet_key)
        self._tenants = TenantRepository()

    async def upsert(
        self, *, tenant_id: str, payload: TenantLLMConfigCreate,
    ) -> TenantLLMConfig:
        """Upsert a tenant's LLM provider config.

        Raises:
            ValueError: tenant does not exist. Caller (API layer)
                translates to ``HTTPException(404)``. Anti-enumeration:
                the same 404 is returned whether the tenant doesn't
                exist OR the tenant exists but the caller can't see it.
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        encrypted = self._cipher.encrypt(payload.api_key)
        return await self._inner.upsert(
            tenant_id=tenant_id,
            provider_name=payload.provider_name,
            encrypted_api_key=encrypted,
            base_url=payload.base_url,
            model=payload.model,
            enabled=payload.enabled,
        )

    async def list(self, tenant_id: str) -> list[TenantLLMConfig]:
        """List all provider configs for a tenant (enabled + disabled).

        Raises:
            ValueError: tenant does not exist (see :meth:`upsert`).
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.list_by_tenant(tenant_id, enabled_only=False)

    async def update(
        self,
        *,
        tenant_id: str,
        provider_name: str,
        payload: TenantLLMConfigUpdate,
    ) -> TenantLLMConfig | None:
        """Update ``enabled`` and/or ``base_url`` without rotating the key.

        Tier 1 Task 1.3. Returns ``None`` when no row matches (the
        API layer maps that to 404 — same anti-enumeration shape as
        the upsert path). The encrypted API key is preserved across
        the update.
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        # Translate ``base_url == ""`` (frontend-friendly clear) into
        # ``None`` (DB NULL). Empty string isn't a valid base URL.
        base_url = payload.base_url
        if base_url == "":
            base_url = None
        # ``model`` (M5.2) — distinguish "omitted" from "explicit null/empty".
        # Pydantic exposes ``model_fields_set`` = field names the client
        # actually sent. If ``model`` was omitted entirely, pass the
        # ``_UNSET`` sentinel so the inner repo skips the column; if it
        # was sent as ``""``, normalise to ``None`` (DB NULL).
        if "model" in payload.model_fields_set:
            model = payload.model
            if model == "":
                model = None
        else:
            model = _UNSET
        # ``enabled`` is bool | None in the schema; pass through as-is.
        return await self._inner.update(
            tenant_id=tenant_id,
            provider_name=provider_name,
            enabled=payload.enabled,
            base_url=base_url,
            model=model,
        )

    async def delete(self, *, tenant_id: str, provider_name: str) -> bool:
        """Remove the (tenant, provider) row. Idempotent.

        Tier 1 Task 1.3. Returns ``True`` if a row was deleted, ``False``
        if there was nothing to delete. The API layer translates that
        to 204 / 404 the same way as other resources.
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.delete_by_tenant_provider(
            tenant_id=tenant_id, provider_name=provider_name,
        )


class AdminTenantBudgetRepository:
    """Admin-facing wrapper around :class:`TenantBudgetRepository`.

    Mirrors :class:`AdminTenantLLMConfigRepository`'s tenant-existence
    check + delegation pattern. No encryption layer — budget config
    carries no secrets (just token counts + an IANA timezone string).
    """

    def __init__(self) -> None:
        self._inner = TenantBudgetRepository()
        self._tenants = TenantRepository()

    async def upsert(
        self, *, tenant_id: str, payload: TenantBudgetCreate,
    ) -> TenantBudget:
        """Upsert a tenant's budget config.

        Raises:
            ValueError: tenant does not exist. Caller (API layer)
                translates to ``HTTPException(404)``. Anti-enumeration:
                the same 404 is returned whether the tenant doesn't
                exist OR the tenant exists but the caller can't see it.
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.upsert(
            tenant_id=tenant_id,
            soft_warn_tokens=payload.soft_warn_tokens,
            hard_cap_tokens=payload.hard_cap_tokens,
            period_anchor_tz=payload.period_anchor_tz,
        )

    async def get(self, tenant_id: str) -> TenantBudget | None:
        """Return the budget row for a tenant (or ``None`` if not set).

        Raises:
            ValueError: tenant does not exist (see :meth:`upsert`).
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.get_by_tenant(tenant_id)


class AdminTenantFallbackChainRepository:
    """Admin-facing wrapper around :class:`TenantFallbackChainRepository`.

    Mirrors :class:`AdminTenantBudgetRepository`'s tenant-existence
    check + delegation pattern. The chain carries no secrets
    (just provider/model/enabled tuples + master ``enabled`` flag) so
    there is no encryption layer.
    """

    def __init__(self) -> None:
        self._inner = TenantFallbackChainRepository()
        self._tenants = TenantRepository()

    async def upsert(
        self,
        *,
        tenant_id: str,
        payload: TenantFallbackChainUpsert,
    ) -> TenantFallbackChain:
        """Upsert a tenant's PRIMARY fallback chain (M5.1).

        Raises:
            ValueError: tenant does not exist. Caller (API layer)
                translates to ``HTTPException(404)``. Anti-enumeration:
                the same 404 is returned whether the tenant doesn't
                exist OR the caller can't see it.
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.upsert(
            tenant_id=tenant_id,
            steps=[s.model_dump() for s in payload.steps],
            enabled=payload.enabled,
        )

    async def get(self, tenant_id: str) -> TenantFallbackChain | None:
        """Return the chain row for a tenant (or ``None`` if not set).

        Raises:
            ValueError: tenant does not exist (see :meth:`upsert`).
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.get(tenant_id)

    async def delete(self, tenant_id: str) -> bool:
        """Remove the tenant's chain. Idempotent.

        Returns ``True`` if a row was deleted, ``False`` if there was
        nothing to delete. The API layer treats both as 204.
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.delete(tenant_id)


__all__ = [
    "AdminTenantBudgetRepository",
    "AdminTenantFallbackChainRepository",
    "AdminTenantLLMConfigRepository",
]