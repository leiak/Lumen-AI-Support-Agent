"""Tests for the OpenAI embedding client (apps.api.src.llm_client.embeddings).

Uses ``unittest.mock.AsyncMock`` to stand in for the SDK client so we can
exercise retry logic without HTTP fixtures.
"""
from unittest.mock import AsyncMock, MagicMock

import openai
import pytest

from llm_client.embeddings import EmbeddingError, EmbeddingResult, embed_texts


def _make_embedding_item(embedding: list[float]) -> MagicMock:
    """Build a mock for an OpenAI ``Embedding`` response item."""
    item = MagicMock()
    item.embedding = embedding
    item.index = 0  # not inspected but set for realism
    return item


def _make_response(
    *, vectors: list[list[float]], model: str = "text-embedding-3-small"
) -> MagicMock:
    """Build a mock ``CreateEmbeddingResponse`` matching the real shape."""
    resp = MagicMock()
    resp.data = [_make_embedding_item(v) for v in vectors]
    resp.model = model
    resp.usage = MagicMock(prompt_tokens=sum(len(v) for v in vectors), total_tokens=42)
    return resp


def _mock_client(
    *,
    side_effect: list[object] | object | None = None,
    return_value: MagicMock | None = None,
) -> MagicMock:
    """Build a mock ``openai.AsyncOpenAI`` whose ``embeddings.create`` is async.

    Pass either ``side_effect`` (list of exceptions/return values applied
    sequentially) or a single ``return_value``.
    """
    client = MagicMock(spec=openai.AsyncOpenAI)
    create = AsyncMock()
    if side_effect is not None:
        create.side_effect = side_effect
    elif return_value is not None:
        create.return_value = return_value
    client.embeddings.create = create
    return client


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


async def test_embed_texts_returns_vectors_in_input_order() -> None:
    vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6], [0.7, 0.8, 0.9]]
    client = _mock_client(return_value=_make_response(vectors=vectors))

    result = await embed_texts(
        texts=["a", "b", "c"],
        client=client,  # type: ignore[arg-type]
    )

    assert result.vectors == vectors
    # Verify the SDK was called once with the inputs in order.
    client.embeddings.create.assert_awaited_once()
    call_kwargs = client.embeddings.create.await_args.kwargs
    assert call_kwargs["input"] == ["a", "b", "c"]


async def test_embed_texts_returns_correct_dimensions() -> None:
    n_texts = 5
    dim = 1536  # text-embedding-3-small
    vectors = [[0.0] * dim for _ in range(n_texts)]
    client = _mock_client(return_value=_make_response(vectors=vectors))

    result = await embed_texts(
        texts=[f"text-{i}" for i in range(n_texts)],
        client=client,  # type: ignore[arg-type]
    )

    assert len(result.vectors) == n_texts
    for vec in result.vectors:
        assert len(vec) == dim


async def test_embed_texts_sums_token_usage() -> None:
    # Build two batches of 2048 — each batch returns its own usage, which
    # must be aggregated by embed_texts.
    batch_size = 2048
    n_batches = 2
    texts = [f"t-{i}" for i in range(batch_size * n_batches)]

    per_batch_prompt_tokens = 100
    per_batch_total_tokens = 150

    def make_batch_response(_call: object) -> MagicMock:
        resp = MagicMock()
        resp.data = [_make_embedding_item([0.0] * 1536) for _ in range(batch_size)]
        resp.model = "text-embedding-3-small"
        resp.usage = MagicMock(
            prompt_tokens=per_batch_prompt_tokens,
            total_tokens=per_batch_total_tokens,
        )
        return resp

    # First call → batch 1, second call → batch 2
    side_effects = [
        make_batch_response(None),
        make_batch_response(None),
    ]
    client = _mock_client(side_effect=side_effects)

    result = await embed_texts(
        texts=texts,
        client=client,  # type: ignore[arg-type]
    )

    assert len(result.vectors) == batch_size * n_batches
    assert result.prompt_tokens == per_batch_prompt_tokens * n_batches
    assert result.total_tokens == per_batch_total_tokens * n_batches
    assert client.embeddings.create.await_count == n_batches


async def test_embed_texts_uses_configured_model() -> None:
    client = _mock_client(
        return_value=_make_response(
            vectors=[[0.0] * 1536],
            model="text-embedding-3-large",
        ),
    )

    await embed_texts(
        texts=["x"],
        model="text-embedding-3-large",
        client=client,  # type: ignore[arg-type]
    )

    call_kwargs = client.embeddings.create.await_args.kwargs
    assert call_kwargs["model"] == "text-embedding-3-large"


# ---------------------------------------------------------------------------
# Retry behavior
# ---------------------------------------------------------------------------


async def test_embed_texts_retries_on_rate_limit() -> None:
    """Two RateLimitErrors then success: embed_texts should return successfully."""
    vectors = [[0.1, 0.2, 0.3]]
    success = _make_response(vectors=vectors)
    side_effects: list[object] = [
        openai.RateLimitError(
            "rate limited",
            response=MagicMock(status_code=429),
            body=None,
        ),
        openai.RateLimitError(
            "rate limited again",
            response=MagicMock(status_code=429),
            body=None,
        ),
        success,
    ]
    client = _mock_client(side_effect=side_effects)

    # Use a real provider instance so retries actually run; mock the SDK
    # via AsyncMock and pass it in. We assert attempts below.
    from llm_client.providers.openai_embedding_provider import OpenAIEmbeddingProvider

    provider = OpenAIEmbeddingProvider(  # type: ignore[arg-type]
        client=client, max_retries=3, base_backoff_seconds=0
    )
    result = await provider.embed(["a"], "text-embedding-3-small")

    assert result.vectors == vectors
    assert client.embeddings.create.await_count == 3


async def test_embed_texts_does_not_retry_on_other_errors() -> None:
    """APIError (non-rate-limit) should raise EmbeddingError after a single attempt."""
    api_error = openai.APIError(
        "server broke",
        request=MagicMock(),
        body=None,
    )
    client = _mock_client(side_effect=api_error)

    with pytest.raises(EmbeddingError):
        await embed_texts(
            texts=["a"],
            client=client,  # type: ignore[arg-type]
        )

    assert client.embeddings.create.await_count == 1


async def test_embed_texts_raises_embedding_error_on_rate_limit_exhausted() -> None:
    """If retries are exhausted on rate limit, EmbeddingError is raised."""
    side_effects: list[object] = [
        openai.RateLimitError(
            "rl1", response=MagicMock(status_code=429), body=None
        ),
        openai.RateLimitError(
            "rl2", response=MagicMock(status_code=429), body=None
        ),
        openai.RateLimitError(
            "rl3", response=MagicMock(status_code=429), body=None
        ),
        openai.RateLimitError(
            "rl4", response=MagicMock(status_code=429), body=None
        ),
    ]
    client = _mock_client(side_effect=side_effects)

    with pytest.raises(EmbeddingError, match="rate limit"):
        await embed_texts(
            texts=["a"],
            client=client,  # type: ignore[arg-type]
        )

    # 1 initial + 3 retries = 4 total attempts
    assert client.embeddings.create.await_count == 4


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


async def test_embed_empty_list_returns_empty_result() -> None:
    """Zero inputs → empty result, no API call."""
    client = _mock_client()

    result = await embed_texts(
        texts=[],
        client=client,  # type: ignore[arg-type]
    )

    assert result == EmbeddingResult(
        vectors=[], model="text-embedding-3-small", prompt_tokens=0, total_tokens=0
    )
    client.embeddings.create.assert_not_awaited()


async def test_embed_batch_size_limit() -> None:
    """Inputs > 2048 texts must be split into multiple batches."""
    batch_size = 2048
    n_batches = 3
    texts = [f"t-{i}" for i in range(batch_size * n_batches)]
    assert len(texts) == 2048 * 3  # 6144

    side_effects: list[MagicMock] = []
    for _ in range(n_batches):
        resp = MagicMock()
        resp.data = [_make_embedding_item([float(i)]) for i in range(batch_size)]
        resp.model = "text-embedding-3-small"
        resp.usage = MagicMock(prompt_tokens=10, total_tokens=20)
        side_effects.append(resp)
    client = _mock_client(side_effect=side_effects)

    result = await embed_texts(
        texts=texts,
        client=client,  # type: ignore[arg-type]
    )

    assert len(result.vectors) == batch_size * n_batches
    assert client.embeddings.create.await_count == n_batches
    # Each batch call should receive exactly `batch_size` inputs.
    for call in client.embeddings.create.await_args_list:
        assert len(call.kwargs["input"]) == batch_size