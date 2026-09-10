"""Unit tests for ``knowledge.qdrant_client.ensure_collection``.

The Qdrant client is patched at the boundary (``core.qdrant.get_qdrant_client``)
so the tests stay HTTP-free: we never open a real socket. The contract
under test is the small one of the helper itself — call the right
``get_collection`` / ``create_collection`` methods, in the right order,
and surface the right bool + log level.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog.testing

from knowledge.qdrant_client import (
    DEFAULT_COLLECTION,
    DEFAULT_VECTOR_SIZE,
    ensure_collection,
)


@pytest.fixture
def mock_client(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace ``get_qdrant_client`` with a controllable MagicMock.

    The mock has ``get_collection`` and ``create_collection`` pre-wired
    as ``AsyncMock`` so tests can configure their return values /
    side effects without further setup.
    """
    client = MagicMock()
    client.get_collection = AsyncMock()
    client.create_collection = AsyncMock()
    monkeypatch.setattr(
        "knowledge.qdrant_client.get_qdrant_client",
        lambda: client,
    )
    return client


async def test_ensure_collection_returns_true_when_exists(
    mock_client: MagicMock,
) -> None:
    """If ``get_collection`` succeeds the collection already exists -> True.

    ``create_collection`` must NOT be called in this path — the whole
    point of the probe is to avoid an unnecessary write (and the
    race-with-another-creator footgun).
    """
    mock_client.get_collection.return_value = MagicMock()

    result = await ensure_collection(name=DEFAULT_COLLECTION, vector_size=DEFAULT_VECTOR_SIZE)

    assert result is True
    mock_client.get_collection.assert_awaited_once_with(collection_name=DEFAULT_COLLECTION)
    mock_client.create_collection.assert_not_awaited()


async def test_ensure_collection_returns_true_after_create(
    mock_client: MagicMock,
) -> None:
    """If ``get_collection`` raises (not found) and create succeeds -> True.

    This is the cold-boot path: first process to come up creates the
    collection. We assert the ``create_collection`` call carries the
    right ``collection_name`` and that ``VectorParams.size`` matches.
    """
    mock_client.get_collection.side_effect = Exception(
        "Not found: collection 'article_chunks'"
    )
    mock_client.create_collection.return_value = MagicMock()

    result = await ensure_collection(name=DEFAULT_COLLECTION, vector_size=DEFAULT_VECTOR_SIZE)

    assert result is True
    mock_client.get_collection.assert_awaited_once_with(collection_name=DEFAULT_COLLECTION)
    mock_client.create_collection.assert_awaited_once()
    call_kwargs = mock_client.create_collection.await_args.kwargs
    assert call_kwargs["collection_name"] == DEFAULT_COLLECTION
    # VectorParams is a pydantic model; .size is a public attribute.
    assert call_kwargs["vectors_config"].size == DEFAULT_VECTOR_SIZE


async def test_ensure_collection_returns_false_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the client cannot be obtained at all, return False + WARNING log.

    Simulates the realistic failure mode where ``get_qdrant_client``
    itself blows up (bad config, missing settings, etc.) — the
    ``except`` at the top of ``ensure_collection`` catches this and
    logs a WARNING without leaking the underlying message.
    """
    def _boom() -> None:
        raise RuntimeError("simulated config failure")

    monkeypatch.setattr("knowledge.qdrant_client.get_qdrant_client", _boom)

    with structlog.testing.capture_logs() as logs:
        result = await ensure_collection(name=DEFAULT_COLLECTION, vector_size=DEFAULT_VECTOR_SIZE)

    assert result is False
    # Exactly one WARNING from the top-level except, with PII-safe fields.
    warning_logs = [entry for entry in logs if entry["log_level"] == "warning"]
    assert len(warning_logs) == 1
    assert warning_logs[0]["event"] == "qdrant.collection.ensure_failed"
    assert warning_logs[0]["collection"] == DEFAULT_COLLECTION
    assert warning_logs[0]["error_type"] == "RuntimeError"
    # And crucially: the exception message must NOT appear in the log
    # payload (it could carry infra URLs, auth hints, etc.).
    assert "simulated config failure" not in str(warning_logs[0])
