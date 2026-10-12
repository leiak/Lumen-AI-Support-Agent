"""Unit tests for ``_diff_upsert`` (M5.3 audit log diff helper).

Pure function — no DB. Mirrors the 9 scenarios from spec §10.2.
"""
from __future__ import annotations

from admin.repository import _diff_upsert
from llm_client.tenant_config_models import TenantLLMConfig


def _row(**kw: object) -> TenantLLMConfig:
    """Build a TenantLLMConfig stub (no DB) with the given fields set."""
    base: dict[str, object] = {
        "id": "x", "tenant_id": "t", "provider_name": "anthropic",
        "encrypted_api_key": b"ct", "base_url": None, "model": None,
        "enabled": True, "created_at": None, "updated_at": None,
    }
    base.update(kw)
    return TenantLLMConfig(**base)  # type: ignore[arg-type]


def test_post_insert_returns_four_rows() -> None:
    diffs = _diff_upsert(
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
    assert api_key_diff[2] == "abc123..."


def test_post_update_changes_all_returns_four() -> None:
    old = _row(
        base_url="https://old", model="claude-3-5-sonnet-20241022", enabled=False,
    )
    diffs = _diff_upsert(
        old=old,
        payload_model_fields_set=None,
        new_base_url="https://new",
        new_model="claude-haiku-4-5",
        new_enabled=True,
        new_api_key_sha256="newhash",
    )
    assert len(diffs) == 4


def test_post_update_one_field_changed_returns_one() -> None:
    old = _row(
        base_url="https://same", model="claude-3-5-sonnet-20241022", enabled=True,
    )
    diffs = _diff_upsert(
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
    old = _row(
        base_url="https://same", model="claude-3-5-sonnet-20241022", enabled=True,
    )
    diffs = _diff_upsert(
        old=old,
        payload_model_fields_set=None,
        new_base_url="https://same",
        new_model="claude-3-5-sonnet-20241022",
        new_enabled=True,
        new_api_key_sha256="samehash",  # Even identical plaintext → same hash → no diff
        old_api_key_sha256="samehash",  # wrapper fetched prior hash from latest audit row
    )
    assert diffs == []


def test_patch_two_fields_changed_returns_two() -> None:
    old = _row(model="claude-3-5-sonnet-20241022", enabled=True)
    diffs = _diff_upsert(
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
    old = _row(model="claude-haiku-4-5", enabled=False)
    diffs = _diff_upsert(
        old=old,
        payload_model_fields_set={"model", "enabled"},
        new_base_url=None,
        new_model="claude-haiku-4-5",  # same as old
        new_enabled=False,  # same as old
        new_api_key_sha256=None,
    )
    assert diffs == []


def test_patch_field_not_in_set_skipped() -> None:
    old = _row(base_url="https://old", model="claude-3-5-sonnet-20241022", enabled=True)
    diffs = _diff_upsert(
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
    old = _row(model="claude-haiku-4-5", enabled=True)
    diffs = _diff_upsert(
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
    diffs = _diff_upsert(
        old=_row(provider_name="anthropic"),
        payload_model_fields_set=set(),  # PATCH with no fields (unusual but valid)
        new_base_url=None,
        new_model=None,
        new_enabled=None,
        new_api_key_sha256=None,
    )
    assert not any(d[0] == "__deleted__" for d in diffs)
