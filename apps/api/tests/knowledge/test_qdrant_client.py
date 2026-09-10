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
from qdrant_client.http.exceptions import UnexpectedResponse

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


def _make_unexpected_response(
    status_code: int,
    reason_phrase: str = "",
    content: bytes = b"",
) -> UnexpectedResponse:
    """Construct a qdrant UnexpectedResponse with the given status code."""
    return UnexpectedResponse(
        status_code=status_code,
        reason_phrase=reason_phrase,
        content=content,
        headers=MagicMock(),
    )


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
    """If ``get_collection`` raises a 404 and create succeeds -> True.

    This is the cold-boot path: first process to come up creates the
    collection. We assert the ``create_collection`` call carries the
    right ``collection_name`` and that ``VectorParams.size`` matches.
    """
    mock_client.get_collection.side_effect = _make_unexpected_response(
        404, reason_phrase="Not Found"
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


async def test_ensure_collection_returns_false_on_connection_error(
    mock_client: MagicMock,
) -> None:
    """Probe-level ConnectionError must return False + WARNING with error_type.

    Regression: a previous version of this helper swallowed ALL
    probe exceptions (including ConnectionError / timeout / auth
    refused) and falsely reported success. The fix narrows the
    except to genuine not-found conditions.
    """
    mock_client.get_collection.side_effect = ConnectionError(
        "[Errno 111] Connection refused"
    )

    with structlog.testing.capture_logs() as logs:
        result = await ensure_collection(
            name=DEFAULT_COLLECTION, vector_size=DEFAULT_VECTOR_SIZE
        )

    assert result is False
    # No create attempt — we never got past the probe.
    mock_client.create_collection.assert_not_awaited()
    warning_logs = [entry for entry in logs if entry["log_level"] == "warning"]
    assert len(warning_logs) == 1
    assert warning_logs[0]["event"] == "qdrant.collection.probe_error"
    assert warning_logs[0]["collection"] == DEFAULT_COLLECTION
    assert warning_logs[0]["error_type"] == "ConnectionError"
    # PII-safe: no URL or exception message in payload.
    assert "Errno 111" not in str(warning_logs[0])


async def test_ensure_collection_returns_false_on_create_validation_error(
    mock_client: MagicMock,
) -> None:
    """Non-409 UnexpectedResponse on create -> False + WARNING with status_code.

    Probe says "not found", so we try to create. The create raises a
    qdrant error that is NOT 409 — e.g. a 400 for bad vector params
    or a 500 from the server. That is a real failure, not a race,
    so we must return False.
    """
    mock_client.get_collection.side_effect = _make_unexpected_response(
        404, reason_phrase="Not Found"
    )
    mock_client.create_collection.side_effect = _make_unexpected_response(
        400, reason_phrase="Bad Request", content=b'{"status":{"error":"..."}}'
    )

    with structlog.testing.capture_logs() as logs:
        result = await ensure_collection(
            name=DEFAULT_COLLECTION, vector_size=DEFAULT_VECTOR_SIZE
        )

    assert result is False
    warning_logs = [entry for entry in logs if entry["log_level"] == "warning"]
    assert len(warning_logs) == 1
    assert warning_logs[0]["event"] == "qdrant.collection.create_failed"
    assert warning_logs[0]["collection"] == DEFAULT_COLLECTION
    assert warning_logs[0]["status_code"] == 400
    # PII-safe: no response body content in payload.
    assert "Bad Request" not in str(warning_logs[0])


async def test_ensure_collection_treats_409_already_exists_as_success(
    mock_client: MagicMock,
) -> None:
    """409 UnexpectedResponse on create -> True + 'race_lost' log.

    When two workers race to create the same collection, the loser's
    create call comes back as 409. The end state (collection exists)
    is what we wanted, so this is a success — but we log it as
    'race_lost' for observability.
    """
    # Probe says missing, then someone else creates it before us.
    mock_client.get_collection.side_effect = _make_unexpected_response(
        404, reason_phrase="Not Found"
    )
    mock_client.create_collection.side_effect = _make_unexpected_response(
        409, reason_phrase="Conflict", content=b'{"status":{"error":"already exists"}}'
    )

    with structlog.testing.capture_logs() as logs:
        result = await ensure_collection(
            name=DEFAULT_COLLECTION, vector_size=DEFAULT_VECTOR_SIZE
        )

    assert result is True
    info_logs = [entry for entry in logs if entry["log_level"] == "info"]
    assert any(entry["event"] == "qdrant.collection.race_lost" for entry in info_logs)
    # No WARNING — race_lost is informational, not a failure.
    warning_logs = [entry for entry in logs if entry["log_level"] == "warning"]
    assert warning_logs == []


async def test_ensure_collection_treats_probe_404_as_missing(
    mock_client: MagicMock,
) -> None:
    """Probe 404 -> falls through to create -> True + 'created' log.

    Verifies the happy cold-boot path: probe fails with 404 (which we
    interpret as 'collection does not exist yet'), then create
    succeeds and the helper returns True with a 'created' log.
    """
    mock_client.get_collection.side_effect = _make_unexpected_response(
        404, reason_phrase="Not Found"
    )
    mock_client.create_collection.return_value = MagicMock()

    with structlog.testing.capture_logs() as logs:
        result = await ensure_collection(
            name=DEFAULT_COLLECTION, vector_size=DEFAULT_VECTOR_SIZE
        )

    assert result is True
    # Exactly one INFO event — the 'created' log. No warning.
    info_events = [e["event"] for e in logs if e["log_level"] == "info"]
    assert "qdrant.collection.created" in info_events
    warning_events = [e["event"] for e in logs if e["log_level"] == "warning"]
    assert warning_events == []
