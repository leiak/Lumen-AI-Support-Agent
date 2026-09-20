"""Tests for Doubao vision embedder (M2.B / Stage 17)."""
import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from knowledge.multimodal.embedder import (
    DoubaoVisionEmbedder, VisionEmbedder, EmbeddingResult,
)


@pytest.fixture
def embedder():
    return DoubaoVisionEmbedder(
        api_key="test-key",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-embedding-vision",
    )


@pytest.mark.asyncio
async def test_encode_image_returns_vector(embedder):
    """Image bytes → 1024-dim float vector."""
    fake_response = {
        "data": [{"embedding": [0.1] * 1024}],
        "model": "doubao-embedding-vision",
        "usage": {"prompt_tokens": 100, "total_tokens": 100},
    }

    with patch.object(embedder, "_post") as mock_post:
        mock_post.return_value = fake_response

        result = await embedder.encode(b"fake-jpeg-bytes", mime_type="image/jpeg")

    assert isinstance(result, EmbeddingResult)
    assert len(result.vector) == 1024
    assert all(isinstance(x, float) for x in result.vector)
    assert result.model == "doubao-embedding-vision"


@pytest.mark.asyncio
async def test_encode_retries_on_5xx(embedder):
    """5xx → retry up to 3 times."""
    import httpx
    call_count = 0
    async def fake_post(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.HTTPStatusError("503", request=MagicMock(), response=MagicMock(status_code=503))
        return {"data": [{"embedding": [0.0] * 1024}], "model": "m", "usage": {"prompt_tokens": 0, "total_tokens": 0}}

    with patch.object(embedder, "_post", side_effect=fake_post):
        result = await embedder.encode(b"bytes", mime_type="image/png")

    assert call_count == 3
    assert len(result.vector) == 1024


@pytest.mark.asyncio
async def test_encode_returns_empty_when_api_key_missing():
    """Empty api_key → return zero vector + warning (graceful degradation)."""
    embedder = DoubaoVisionEmbedder(api_key="", base_url="x", model="y")
    result = await embedder.encode(b"data", mime_type="image/png")
    assert len(result.vector) == 1024
    assert all(v == 0.0 for v in result.vector)


def test_embedder_dim_is_1024():
    """The vector dimension must match Qdrant collection config."""
    embedder = DoubaoVisionEmbedder(api_key="x", base_url="x", model="x")
    assert embedder.dimension == 1024