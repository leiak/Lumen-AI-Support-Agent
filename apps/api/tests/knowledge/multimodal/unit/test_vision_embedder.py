"""Tests for Doubao vision embedder (M2.B / Stage 17)."""
import pytest
import pytest_asyncio
from unittest.mock import patch, AsyncMock, MagicMock
from knowledge.multimodal.embedder import (
    DoubaoVisionEmbedder, VisionEmbedder, EmbeddingResult,
)


@pytest_asyncio.fixture
async def embedder():
    """Async fixture — closes the long-lived httpx client on teardown."""
    e = DoubaoVisionEmbedder(
        api_key="test-key",
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-embedding-vision",
    )
    yield e
    await e.aclose()


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
    try:
        result = await embedder.encode(b"data", mime_type="image/png")
    finally:
        await embedder.aclose()
    assert len(result.vector) == 1024
    assert all(v == 0.0 for v in result.vector)


def test_embedder_dim_is_1024():
    """The vector dimension must match Qdrant collection config."""
    # ClassVar — read directly from the class, no need to instantiate.
    assert DoubaoVisionEmbedder.dimension == 1024
    assert VisionEmbedder.dimension == 1024


# --------------------------------------------------------------------------
# Stage 19 / M3 / tech-debt #19 — tests for new adapters + factory
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_clip_encode_returns_768_dim_vector():
    """OpenAI CLIP returns 768-dim vectors."""
    from knowledge.multimodal.embedder import OpenAIVisionEmbedder

    e = OpenAIVisionEmbedder(
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        model="clip-vit-large-patch14",
    )
    try:
        with patch.object(e, "_post", new=AsyncMock(return_value={
            "data": [{"embedding": [0.1] * 768}],
            "model": "clip-vit-large-patch14",
        })):
            result = await e.encode(b"jpeg-bytes", mime_type="image/jpeg")
        assert len(result.vector) == 768
        assert result.model == "clip-vit-large-patch14"
    finally:
        await e.aclose()


@pytest.mark.asyncio
async def test_openai_clip_empty_api_key_returns_zero_vector():
    """Graceful degradation: no key → 768-dim zero vector."""
    from knowledge.multimodal.embedder import OpenAIVisionEmbedder

    e = OpenAIVisionEmbedder(api_key="", base_url="x", model="y")
    try:
        result = await e.encode(b"data", mime_type="image/png")
    finally:
        await e.aclose()
    assert len(result.vector) == 768
    assert all(v == 0.0 for v in result.vector)


@pytest.mark.asyncio
async def test_voyage_encode_returns_1024_dim_vector():
    """Voyage returns 1024-dim vectors."""
    from knowledge.multimodal.embedder import VoyageVisionEmbedder

    e = VoyageVisionEmbedder(
        api_key="test-key",
        model="voyage-multimodal-3",
    )
    try:
        with patch.object(e, "_post", new=AsyncMock(return_value={
            "data": [{"embedding": [0.1] * 1024}],
            "model": "voyage-multimodal-3",
        })):
            result = await e.encode(b"png-bytes", mime_type="image/png")
        assert len(result.vector) == 1024
        assert result.model == "voyage-multimodal-3"
    finally:
        await e.aclose()


@pytest.mark.asyncio
async def test_voyage_encode_sends_inputs_shape():
    """Voyage uses 'inputs' (plural) with nested 'content' array."""
    from knowledge.multimodal.embedder import VoyageVisionEmbedder

    captured: list[dict] = []

    async def fake_post(payload: dict) -> dict:
        captured.append(payload)
        return {"data": [{"embedding": [0.0] * 1024}]}

    e = VoyageVisionEmbedder(api_key="k", model="voyage-multimodal-3")
    try:
        with patch.object(e, "_post", side_effect=fake_post):
            await e.encode(b"data", mime_type="image/png")
    finally:
        await e.aclose()

    assert len(captured) == 1
    payload = captured[0]
    assert "inputs" in payload
    assert payload["inputs"][0]["content"][0]["type"] == "image_base64"
    assert payload["inputs"][0]["content"][0]["media_type"] == "image/png"


def test_factory_returns_doubao_by_default():
    """Default vision_provider is doubao."""
    from knowledge.multimodal.embedder import (
        DoubaoVisionEmbedder,
        OpenAIVisionEmbedder,
        VoyageVisionEmbedder,
        get_vision_embedder,
    )
    from core.config import Settings

    settings = Settings()
    embedder = get_vision_embedder(settings)
    assert isinstance(embedder, DoubaoVisionEmbedder)


def test_factory_returns_openai_clip_for_clip_provider():
    from knowledge.multimodal.embedder import (
        OpenAIVisionEmbedder,
        get_vision_embedder,
    )
    from core.config import Settings

    # pydantic-settings v2 with ``alias=`` honors kwargs only via the
    # alias name (unless populate_by_name is enabled). Pass the alias
    # names explicitly so the value isn't silently ignored.
    settings = Settings(
        VISION_PROVIDER="openai_clip",
        OPENAI_CLIP_API_KEY="k",
    )
    embedder = get_vision_embedder(settings)
    assert isinstance(embedder, OpenAIVisionEmbedder)


def test_factory_returns_voyage_for_voyage_provider():
    from knowledge.multimodal.embedder import VoyageVisionEmbedder, get_vision_embedder
    from core.config import Settings

    settings = Settings(
        VISION_PROVIDER="voyage",
        VOYAGE_API_KEY="k",
    )
    embedder = get_vision_embedder(settings)
    assert isinstance(embedder, VoyageVisionEmbedder)


def test_factory_raises_on_unknown_provider():
    from knowledge.multimodal.embedder import get_vision_embedder
    from core.config import Settings

    # ``vision_provider`` is a Literal so Settings() rejects an unknown
    # value at construction. Force the attribute to simulate a config
    # that slipped past validation (e.g. an old process started before
    # the Literal was narrowed) — the factory still must fail loud.
    settings = Settings()
    object.__setattr__(settings, "vision_provider", "bogus")
    with pytest.raises(ValueError, match="unknown vision_provider"):
        get_vision_embedder(settings)


def test_adapter_dimensions_match_known_values():
    """Sanity: dimensions are the values the Qdrant collections will be sized with."""
    from knowledge.multimodal.embedder import (
        DoubaoVisionEmbedder,
        OpenAIVisionEmbedder,
        VoyageVisionEmbedder,
    )

    assert DoubaoVisionEmbedder.dimension == 1024
    assert OpenAIVisionEmbedder.dimension == 768
    assert VoyageVisionEmbedder.dimension == 1024