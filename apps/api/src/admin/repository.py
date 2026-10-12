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

M5.3 — wrapper OWNS its own session and writes audit rows atomically
with each mutation (single TX, all-or-nothing). The ``changed_by``
identity is threaded from the route layer via ``claims["sub"]``;
``api_key_sha256`` is threaded on POST so the audit log records
"who rotated the key when" without ever seeing plaintext.
"""
from __future__ import annotations

from admin.schemas.budget import TenantBudgetCreate
from admin.schemas.fallback_chain import TenantFallbackChainUpsert
from admin.schemas.tenant_llm_config import TenantLLMConfigCreate, TenantLLMConfigUpdate
from budget.models import TenantBudget
from budget.repository import TenantBudgetRepository
from core.config import get_settings
from core.id_gen import new_id
from observability import traced
from llm_client.tenant_config_audit_models import (
    TenantLLMConfigAuditLog,
    TenantLLMConfigAuditRepository,
)
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
    is_insert: bool,
    payload_model_fields_set: set[str] | None,
    old_base_url: str | None,
    old_model: str | None,
    old_enabled: bool | None,
    new_base_url: str | None,
    new_model: str | None,
    new_enabled: bool | None,
    new_api_key_sha256: str | None,
    old_api_key_sha256: str | None = None,
) -> list[tuple[str, str | None, str | None]]:
    """Produce list of (field, old_repr, new_repr) audit tuples.

    Behavior (M5.3 spec §6.5):
    - POST (insert): ``payload_model_fields_set=None`` and
      ``is_insert=True`` → diff all fields. ``old_*`` values are
      ignored (or the literal "sha256:initial" for api_key).
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
    Stored in the audit row with the literal prefix ``sha256:`` —
    so this parameter is always RAW hex (no prefix) to make the
    equality / no-op suppression check straightforward. When
    provided AND equal to ``new_api_key_sha256`` the helper emits no
    api_key diff (no-op rotation = no audit row).

    Why ``old_*`` are explicit parameters instead of an ORM
    object — see :class:`TenantLLMConfigRow` docstring. SQLAlchemy
    identity map returns the SAME Python object from ``UPDATE ...
    RETURNING``, so reading ``old_row.model`` after the inner
    ``upsert/update`` would see the NEW value. The wrapper snapshots
    the OLD attributes into plain values and passes them in here.
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
        # Stored repr: ``sha256:<raw-hex>`` per spec §5.2.
        new_repr = f"sha256:{new_api_key_sha256}"
        if is_insert:
            diffs.append(("api_key", "sha256:initial", new_repr))
        elif old_api_key_sha256 == new_api_key_sha256:
            # No-op rotation — same plaintext → no diff. Note: the
            # stored old/new reprs would have been identical, which
            # carries no information, so we suppress the audit row.
            pass
        else:
            # Real rotation (or no prior hash available). The
            # ``old=`` repr stored here is the prior hash when we
            # have it; NULL otherwise. Auditors without the prior
            # hash can still recover it via the chain of audit
            # rows' ``new_value``.
            old_repr = (
                f"sha256:{old_api_key_sha256}"
                if old_api_key_sha256 is not None
                else None
            )
            diffs.append(("api_key", old_repr, new_repr))

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
        if is_insert:
            # Insert — every non-key field is "new". The diff is
            # always written because the value is brand-new (None →
            # something). We don't try to compare against the
            # pre-existing DB row (there is none).
            diffs.append((field_name, None, new_value))
        else:
            if new_value != old_value:
                diffs.append((field_name, old_value, new_value))

    _maybe("base_url", new_base_url, old_base_url)
    _maybe("model", new_model, old_model)
    _maybe(
        "enabled",
        str(new_enabled) if new_enabled is not None else None,
        str(old_enabled) if old_enabled is not None else None,
    )
    return diffs


# --- M5.3 Task 3 ---
#
# ``TenantLLMConfigRow`` is a tiny immutable snapshot of the
# OLD attribute values the wrapper hands to ``_diff_upsert``.
# It exists because SQLAlchemy's identity map returns the SAME ORM
# object from ``UPDATE ... RETURNING``: calling
# ``inner.upsert(...)`` / ``inner.update(...)`` mutates ``old_row``
# in-place, so reading ``old_row.model`` AFTER the mutation would
# return the NEW value. The wrapper therefore snapshots
# ``base_url / model / enabled`` BEFORE the inner call and hands the
# snapshot to the helper. ``provider_name`` is captured too so the
# DELETE path can keep its provider-name sentinel stable.
class TenantLLMConfigRow:
    """Immutable snapshot — small surface for ``_diff_upsert`` only."""

    __slots__ = ("base_url", "enabled", "model", "provider_name")

    def __init__(
        self,
        *,
        base_url: str | None,
        model: str | None,
        enabled: bool | None,
        provider_name: str | None = None,
    ) -> None:
        self.base_url = base_url
        self.model = model
        self.enabled = enabled
        self.provider_name = provider_name


def _log_audit_mutation(
    tenant_id: str,
    provider_name: str,
    action: str,
    diffs: list[tuple[str, str | None, str | None]],
    changed_by: str,
) -> None:
    """Emit one INFO log per mutation (M5.3 spec §12).

    Uses Python's standard logging via the ``__name__`` logger so
    structlog picks it up at the project level. We do NOT log
    ``old_value`` or ``new_value`` for the ``api_key`` field — only
    the field name + the changed_by identity. This keeps the log
    line PII-safe (no key hash bits, even though hashes are not
    plaintext).
    """
    import logging

    logger = logging.getLogger(__name__)
    logger.info(
        "audit.llm_config.changed",
        extra={
            "tenant_id": tenant_id,
            "provider": provider_name,
            "action": action,
            "fields_changed": [f for f, _, _ in diffs],
            "rows_written": len(diffs),
            "changed_by": changed_by,
        },
    )


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
        self._audit_repo = TenantLLMConfigAuditRepository()

    @traced("admin.llm_config.upsert")
    async def upsert(
        self,
        *,
        tenant_id: str,
        payload: TenantLLMConfigCreate,
        changed_by: str,
        api_key_sha256: str | None = None,
    ) -> TenantLLMConfig:
        """Upsert a tenant's LLM provider config (M5.3 atomic audit).

        On POST the route layer must compute ``api_key_sha256`` from
        the plaintext and pass it down; it is recorded in the audit
        log so operators can see "who rotated the key when" without
        the plaintext ever being persisted.

        All three steps — old_row fetch, mutation, audit INSERT — run
        in a single transaction. Any failure rolls back the whole
        thing (see spec §4 + §8.4).

        Raises:
            ValueError: tenant does not exist. Caller (API layer)
                translates to ``HTTPException(404)``.
        """
        from core.database import get_sessionmaker

        sm = get_sessionmaker()
        async with sm() as session:
            tenant = await self._tenants.get_by_id(tenant_id)
            if tenant is None:
                raise ValueError(f"unknown tenant: {tenant_id!r}")
            encrypted = self._cipher.encrypt(payload.api_key)
            old_row = await self._inner.get_by_tenant_provider_for_provider(
                session=session, tenant_id=tenant_id, provider_name=payload.provider_name,
            )
            is_insert = old_row is None
            # M5.3 Task 2 handoff: read the previous api_key audit
            # row so _diff_upsert can suppress no-op rotations. The
            # stored repr is ``sha256:<hex>``; strip the prefix so
            # the helper compares raw hex against the new hash.
            old_api_key_sha256 = await self._audit_repo.latest_field_value(
                session, tenant_id, payload.provider_name, "api_key",
            )
            if old_api_key_sha256 is not None and old_api_key_sha256.startswith("sha256:"):
                old_api_key_sha256 = old_api_key_sha256[len("sha256:"):]
            # Snapshot OLD attributes BEFORE the inner mutation —
            # SQLAlchemy identity map returns the SAME Python object
            # from ``UPDATE ... RETURNING`` and mutates it in place,
            # so reading ``old_row.model`` afterwards would see the
            # NEW value. ``TenantLLMConfigRow`` is the immutable
            # snapshot consumed by ``_diff_upsert``.
            old_snapshot = None
            if old_row is not None:
                old_snapshot = TenantLLMConfigRow(
                    base_url=old_row.base_url,
                    model=old_row.model,
                    enabled=old_row.enabled,
                    provider_name=old_row.provider_name,
                )
            new_row = await self._inner.upsert(
                tenant_id=tenant_id,
                provider_name=payload.provider_name,
                encrypted_api_key=encrypted,
                base_url=payload.base_url,
                enabled=payload.enabled,
                model=payload.model,
                session=session,
            )
            diffs = _diff_upsert(
                is_insert=is_insert,
                payload_model_fields_set=None,  # POST — diff all fields
                old_base_url=old_snapshot.base_url if old_snapshot else None,
                old_model=old_snapshot.model if old_snapshot else None,
                old_enabled=old_snapshot.enabled if old_snapshot else None,
                new_base_url=new_row.base_url,
                new_model=new_row.model,
                new_enabled=new_row.enabled,
                new_api_key_sha256=api_key_sha256,
                old_api_key_sha256=old_api_key_sha256,
            )
            if diffs:
                await self._audit_repo.insert_many(
                    session,
                    [
                        TenantLLMConfigAuditLog(
                            id=new_id(),
                            tenant_id=tenant_id,
                            provider_name=payload.provider_name,
                            field=f,
                            old_value=o,
                            new_value=n,
                            changed_by=changed_by,
                        )
                        for (f, o, n) in diffs
                    ],
                )
            await session.commit()
            await session.refresh(new_row)
        _log_audit_mutation(tenant_id, payload.provider_name, "upsert", diffs, changed_by)
        return new_row

    async def list(self, tenant_id: str) -> list[TenantLLMConfig]:
        """List all provider configs for a tenant (enabled + disabled).

        Raises:
            ValueError: tenant does not exist (see :meth:`upsert`).
        """
        tenant = await self._tenants.get_by_id(tenant_id)
        if tenant is None:
            raise ValueError(f"unknown tenant: {tenant_id!r}")
        return await self._inner.list_by_tenant(tenant_id, enabled_only=False)

    @traced("admin.llm_config.update")
    async def update(
        self,
        *,
        tenant_id: str,
        provider_name: str,
        payload: TenantLLMConfigUpdate,
        changed_by: str,
    ) -> TenantLLMConfig | None:
        """Update ``enabled`` and/or ``base_url`` without rotating the key.

        M5.3: writes 0-N audit rows depending on which fields actually
        changed. Single-transaction atomicity (see spec §8.2).

        Returns ``None`` when no row matches (the API layer maps that
        to 404 — same anti-enumeration shape as the upsert path).
        """
        from core.database import get_sessionmaker

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
        model = payload.model
        if "model" in payload.model_fields_set:
            if model == "":
                model = None
        else:
            model = _UNSET

        sm = get_sessionmaker()
        async with sm() as session:
            tenant = await self._tenants.get_by_id(tenant_id)
            if tenant is None:
                raise ValueError(f"unknown tenant: {tenant_id!r}")
            old_row = await self._inner.get_by_tenant_provider_for_provider(
                session=session, tenant_id=tenant_id, provider_name=provider_name,
            )
            if old_row is None:
                await session.rollback()
                return None
            # Snapshot OLD attributes before the inner update mutates
            # them via identity map (see ``_diff_upsert`` docstring).
            old_snapshot = TenantLLMConfigRow(
                base_url=old_row.base_url,
                model=old_row.model,
                enabled=old_row.enabled,
                provider_name=old_row.provider_name,
            )
            new_row = await self._inner.update(
                tenant_id=tenant_id,
                provider_name=provider_name,
                enabled=payload.enabled,
                base_url=base_url,
                model=model,
                session=session,
            )
            if new_row is None:
                await session.rollback()
                return None
            diffs = _diff_upsert(
                is_insert=False,
                payload_model_fields_set=payload.model_fields_set,
                old_base_url=old_snapshot.base_url,
                old_model=old_snapshot.model,
                old_enabled=old_snapshot.enabled,
                new_base_url=new_row.base_url,
                new_model=new_row.model,
                new_enabled=new_row.enabled,
                new_api_key_sha256=None,  # PATCH never rotates
                old_api_key_sha256=None,  # not relevant — PATCH never has api_key
            )
            if diffs:
                await self._audit_repo.insert_many(
                    session,
                    [
                        TenantLLMConfigAuditLog(
                            id=new_id(),
                            tenant_id=tenant_id,
                            provider_name=provider_name,
                            field=f,
                            old_value=o,
                            new_value=n,
                            changed_by=changed_by,
                        )
                        for (f, o, n) in diffs
                    ],
                )
            await session.commit()
            await session.refresh(new_row)
        _log_audit_mutation(tenant_id, provider_name, "patch", diffs, changed_by)
        return new_row

    @traced("admin.llm_config.delete")
    async def delete(
        self,
        *,
        tenant_id: str,
        provider_name: str,
        changed_by: str,
    ) -> bool:
        """Remove the (tenant, provider) row. Idempotent.

        M5.3: writes one ``__deleted__`` audit row when a row was
        actually deleted. Returns ``False`` (and 0 audit rows) when
        there was nothing to delete.
        """
        from core.database import get_sessionmaker

        sm = get_sessionmaker()
        async with sm() as session:
            tenant = await self._tenants.get_by_id(tenant_id)
            if tenant is None:
                raise ValueError(f"unknown tenant: {tenant_id!r}")
            old_row = await self._inner.get_by_tenant_provider_for_provider(
                session=session, tenant_id=tenant_id, provider_name=provider_name,
            )
            if old_row is None:
                # Idempotent — nothing to delete. Commit (no-op) and
                # return False. No audit row.
                await session.commit()
                return False
            deleted = await self._inner.delete_by_tenant_provider(
                tenant_id=tenant_id, provider_name=provider_name, session=session,
            )
            if not deleted:
                await session.rollback()
                return False
            diffs = [("__deleted__", provider_name, None)]
            await self._audit_repo.insert_many(
                session,
                [
                    TenantLLMConfigAuditLog(
                        id=new_id(),
                        tenant_id=tenant_id,
                        provider_name=provider_name,
                        field="__deleted__",
                        old_value=provider_name,
                        new_value=None,
                        changed_by=changed_by,
                    )
                ],
            )
            await session.commit()
        _log_audit_mutation(tenant_id, provider_name, "delete", diffs, changed_by)
        return True


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