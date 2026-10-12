"""Unit tests for ``_diff_upsert`` (M5.3 audit log diff helper).

Pure function — no DB. Mirrors the 9 scenarios from spec §10.2.
"""
from __future__ import annotations

from admin.repository import _diff_upsert
from llm_client.tenant_config_models import TenantLLMConfig


def _old(**kw: object):
    """Build a TenantLLMConfig stub (no DB) for the OLD pre-mutation snapshot."""
    base: dict[str, object] = {
        "id": "x", "tenant_id": "t", "provider_name": "anthropic",
        "encrypted_api_key": b"ct", "base_url": None, "model": None,
        "enabled": True, "created_at": None, "updated_at": None,
    }
    base.update(kw)
    return TenantLLMConfig(**base)  # type: ignore[arg-type]


def _call(
    *,
    is_insert: bool,
    old: TenantLLMConfig | None,
    payload_model_fields_set: set[str] | None,
    new_base_url: str | None,
    new_model: str | None,
    new_enabled: bool,
    new_api_key_sha256: str | None,
    old_api_key_sha256: str | None = None,
) -> list[tuple[str, str | None, str | None]]:
    """Ingest an OLD snapshot and pass its attrs into ``_diff_upsert``."""
    return _diff_upsert(
        is_insert=is_insert,
        payload_model_fields_set=payload_model_fields_set,
        old_base_url=old.base_url if old else None,
        old_model=old.model if old else None,
        old_enabled=old.enabled if old else None,
        new_base_url=new_base_url,
        new_model=new_model,
        new_enabled=new_enabled,
        new_api_key_sha256=new_api_key_sha256,
        old_api_key_sha256=old_api_key_sha256,
    )


def test_post_insert_returns_four_rows() -> None:
    diffs = _call(
        is_insert=True,
        old=None,
        payload_model_fields_set=None,
        new_base_url="https://proxy",
        new_model="claude-haiku-4-5",
        new_enabled=True,
        new_api_key_sha256="abc123...",
    )
    fields = {d[0] for d in diffs}
    assert fields == {"api_key", "base_url", "model", "enabled"}
    api_key_diff = next(d for d in diffs if d[0] == "api_key")
    assert api_key_diff[1] == "sha256:initial"
    assert api_key_diff[2] == "sha256:abc123..."


def test_post_update_changes_all_returns_four() -> None:
    old = _old(
        base_url="https://old", model="claude-3-5-sonnet-20241022", enabled=False,
    )
    diffs = _call(
        is_insert=False,
        old=old,
        payload_model_fields_set=None,
        new_base_url="https://new",
        new_model="claude-haiku-4-5",
        new_enabled=True,
        new_api_key_sha256="newhash",
    )
    assert len(diffs) == 4


def test_post_update_one_field_changed_returns_one() -> None:
    old = _old(
        base_url="https://same", model="claude-3-5-sonnet-20241022", enabled=True,
    )
    diffs = _call(
        is_insert=False,
        old=old,
        payload_model_fields_set=None,
        new_base_url="https://same",  # unchanged
        new_model="claude-3-5-sonnet-20241022",  # unchanged
        new_enabled=True,  # unchanged
        new_api_key_sha256="newkeyhash",  # changed
    )
    assert len(diffs) == 1
    assert diffs[0][0] == "api_key"


def test_post_identical_body_returns_zero() -> None:
    old = _old(
        base_url="https://same", model="claude-3-5-sonnet-20241022", enabled=True,
    )
    diffs = _call(
        is_insert=False,
        old=old,
        payload_model_fields_set=None,
        new_base_url="https://same",
        new_model="claude-3-5-sonnet-20241022",
        new_enabled=True,
        new_api_key_sha256="samehash",  # wrapper fetched prior hash, suppress no-op
        old_api_key_sha256="samehash",
    )
    assert diffs == []


def test_patch_two_fields_changed_returns_two() -> None:
    old = _old(model="claude-3-5-sonnet-20241022", enabled=True)
    diffs = _call(
        is_insert=False,
        old=old,
        payload_model_fields_set={"model", "enabled"},
        new_base_url=None,  # ignored (not in fields_set)
        new_model="claude-haiku-4-5",
        new_enabled=False,
        new_api_key_sha256=None,  # PATCH never rotates
    )
    assert len(diffs) == 2
    assert {d[0] for d in diffs} == {"model", "enabled"}


def test_patch_no_actual_change_returns_zero() -> None:
    old = _old(model="claude-haiku-4-5", enabled=False)
    diffs = _call(
        is_insert=False,
        old=old,
        payload_model_fields_set={"model", "enabled"},
        new_base_url=None,
        new_model="claude-haiku-4-5",  # same as old
        new_enabled=False,  # same as old
        new_api_key_sha256=None,
    )
    assert diffs == []


def test_patch_field_not_in_set_skipped() -> None:
    old = _old(base_url="https://old", model="claude-3-5-sonnet-20241022", enabled=True)
    diffs = _call(
        is_insert=False,
        old=old,
        payload_model_fields_set={"model"},  # base_url NOT in set
        new_base_url="https://new",  # but value changed
        new_model="claude-3-5-sonnet-20241022",  # unchanged
        new_enabled=True,
        new_api_key_sha256=None,
    )
    # base_url diff is suppressed (not in fields_set) → 0 rows
    assert diffs == []


def test_patch_clears_to_null_returns_one() -> None:
    old = _old(model="claude-haiku-4-5", enabled=True)
    diffs = _call(
        is_insert=False,
        old=old,
        payload_model_fields_set={"model"},
        new_base_url=None,
        new_model=None,  # explicit clear
        new_enabled=True,
        new_api_key_sha256=None,
    )
    assert len(diffs) == 1
    assert diffs[0] == ("model", "claude-haiku-4-5", None)


def test_delete_path_does_not_use_helper() -> None:
    """DELETE is not handled by _diff_upsert — the wrapper writes the
    ``__deleted__`` row directly. This test is a smoke check that
    the helper does not produce a ``__deleted__`` row on its own."""
    old = _old(provider_name="anthropic")
    diffs = _call(
        is_insert=False,
        old=old,
        payload_model_fields_set=set(),  # PATCH with no fields (unusual but valid)
        new_base_url=None,
        new_model=None,
        new_enabled=False,
        new_api_key_sha256=None,
    )
    assert not any(d[0] == "__deleted__" for d in diffs)
