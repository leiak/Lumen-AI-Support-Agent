# Tech Debt #19 — Multi-Vision Embedding Adapter

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the vision embedder provider-pluggable. Support **Doubao** (existing, default), **OpenAI CLIP ViT-L/14** (768-dim), and **Voyage Multimodal 3** (1024-dim) behind a single `VisionEmbedder` abstract base. The provider is selected at runtime via `settings.vision_provider`.

**Architecture:** Add 2 new concrete embedder classes + a factory function. Use Qdrant collection aliases (`kb_image_vectors` → `kb_image_vectors_doubao` by default) so existing data + tests continue to work without migration. Per-provider collection names: `kb_image_vectors_doubao`, `kb_image_vectors_clip`, `kb_image_vectors_voyage`.

**Tech Stack:** httpx async, dataclasses, pytest, pytest-asyncio.

---

## File Structure

| Path | Role |
|------|------|
| `apps/api/src/knowledge/multimodal/embedder.py` | Add `OpenAIVisionEmbedder`, `VoyageVisionEmbedder`, `get_vision_embedder()` factory |
| `apps/api/src/knowledge/startup.py` | Replace `IMAGE_COLLECTION` const with `get_image_collection_name()`; add alias bootstrap |
| `apps/api/src/knowledge/multimodal/api.py` | Use factory + dynamic collection name |
| `apps/api/src/knowledge/multimodal/retriever.py` | Use factory + dynamic collection name |
| `apps/api/src/core/config.py` | Add `vision_provider`, `openai_clip_*`, `voyage_*` settings |
| `apps/api/tests/knowledge/multimodal/unit/test_vision_embedder.py` | Add tests for new adapters + factory |

No new files. No DB migrations.

---

## Task 1: Add config fields

**Files:**
- Modify: `apps/api/src/core/config.py:160-200` (after the existing `doubao_vision_*` block)

- [ ] **Step 1: Add the new fields**

After the existing `doubao_vision_model` block (around line 172), add:

```python
    # Stage 19 / M3 / tech-debt #19: pluggable vision embedder.
    # ``vision_provider`` selects among Doubao (default, backward-compatible),
    # OpenAI CLIP ViT-L/14 (768-dim), and Voyage Multimodal 3 (1024-dim).
    # Each provider writes to its own Qdrant collection; the legacy
    # ``kb_image_vectors`` name remains an alias for the default
    # (Doubao) collection — see :func:`knowledge.startup.get_image_collection_name`.
    vision_provider: Literal["doubao", "openai_clip", "voyage"] = Field(
        default="doubao", alias="VISION_PROVIDER"
    )

    openai_clip_api_key: str = Field(default="", alias="OPENAI_CLIP_API_KEY")
    openai_clip_base_url: str = Field(
        default="https://api.openai.com/v1",
        alias="OPENAI_CLIP_BASE_URL",
    )
    openai_clip_model: str = Field(
        default="clip-vit-large-patch14",
        alias="OPENAI_CLIP_MODEL",
    )

    voyage_api_key: str = Field(default="", alias="VOYAGE_API_KEY")
    voyage_model: str = Field(
        default="voyage-multimodal-3",
        alias="VOYAGE_MODEL",
    )
```

- [ ] **Step 2: Verify `Literal` is imported**

Check the top of `apps/api/src/core/config.py`. If `from typing import Literal` is missing, add it.

- [ ] **Step 3: Verify settings loads**

Run:
```bash
cd apps/api && python -c "from core.config import get_settings; s = get_settings(); print(s.vision_provider, s.openai_clip_api_key[:4], s.voyage_model)"
```
Expected: `doubao   voyage-multimodal-3` (the api_key is empty so `[:4]` gives blank).

- [ ] **Step 4: Commit**

```bash
git add apps/api/src/core/config.py
git commit -m "feat(config): pluggable vision_provider + OpenAI CLIP + Voyage settings"
```

---

## Task 2: Add `OpenAIVisionEmbedder` and `VoyageVisionEmbedder`

**Files:**
- Modify: `apps/api/src/knowledge/multimodal/embedder.py` (append before the `__all__` line, or after `DoubaoVisionEmbedder`)

- [ ] **Step 1: Read the existing embedder to copy the retry + aclose pattern**

Already loaded. The existing `DoubaoVisionEmbedder` has the long-lived httpx client, `aclose()`, `__aenter__/__aexit__`, retry on 5xx, graceful degradation when api_key is empty. Mirror that pattern exactly.

- [ ] **Step 2: Append the new classes**

At the end of `apps/api/src/knowledge/multimodal/embedder.py` (before the `__all__ = [...]` if present), add:

```python
class OpenAIVisionEmbedder(VisionEmbedder):
    """OpenAI CLIP ViT-L/14 via the OpenAI-compatible /v1/embeddings endpoint.

    CLIP returns 768-dim vectors — distinct from Doubao's 1024 — so
    the Qdrant collection for CLIP writes must be sized accordingly.
    """

    dimension: ClassVar[int] = 768

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "OpenAIVisionEmbedder":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _post(self, payload: dict) -> dict:
        resp = await self._client.post(
            f"{self._base_url}/embeddings",
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def encode(self, image_bytes: bytes, *, mime_type: str) -> EmbeddingResult:
        if not self._api_key:
            logger.warning(
                "vision.embedder.degraded",
                extra={"reason": "missing_api_key", "model": self._model},
            )
            return EmbeddingResult(
                vector=[0.0] * self.dimension, model=self._model
            )

        b64 = base64.b64encode(image_bytes).decode("ascii")
        data_uri = f"data:{mime_type};base64,{b64}"
        payload = {
            "model": self._model,
            "input": [{"type": "image_url", "image_url": {"url": data_uri}}],
        }

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._post(payload)
                vec = resp["data"][0]["embedding"]
                return EmbeddingResult(vector=vec, model=self._model)
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (2 ** attempt))

        logger.warning(
            "vision.embedder.failed",
            extra={
                "model": self._model,
                "error_type": type(last_exc).__name__ if last_exc else "unknown",
            },
        )
        return EmbeddingResult(vector=[0.0] * self.dimension, model=self._model)


class VoyageVisionEmbedder(VisionEmbedder):
    """Voyage Multimodal 3 (voyage-multimodal-3) — 1024-dim.

    Voyage uses a distinct request shape (``inputs`` instead of ``input``,
    nested ``content`` object with explicit media_type). Endpoint is
    ``/v1/multimodalembeddings`` on api.voyageai.com.
    Ref: https://docs.voyageai.com/docs/multimodal-embeddings
    """

    dimension: ClassVar[int] = 1024

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.voyageai.com/v1",
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        super().__init__()
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout_seconds
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=self._timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "VoyageVisionEmbedder":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def _post(self, payload: dict) -> dict:
        resp = await self._client.post(
            f"{self._base_url}/multimodalembeddings",
            json=payload,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        resp.raise_for_status()
        return resp.json()

    async def encode(self, image_bytes: bytes, *, mime_type: str) -> EmbeddingResult:
        if not self._api_key:
            logger.warning(
                "vision.embedder.degraded",
                extra={"reason": "missing_api_key", "model": self._model},
            )
            return EmbeddingResult(
                vector=[0.0] * self.dimension, model=self._model
            )

        b64 = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": self._model,
            "inputs": [
                {
                    "content": [
                        {
                            "type": "image_base64",
                            "image_base64": b64,
                            "media_type": mime_type,
                        }
                    ]
                }
            ],
        }

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                resp = await self._post(payload)
                vec = resp["data"][0]["embedding"]
                return EmbeddingResult(vector=vec, model=self._model)
            except httpx.HTTPError as e:
                last_exc = e
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(0.5 * (2 ** attempt))

        logger.warning(
            "vision.embedder.failed",
            extra={
                "model": self._model,
                "error_type": type(last_exc).__name__ if last_exc else "unknown",
            },
        )
        return EmbeddingResult(vector=[0.0] * self.dimension, model=self._model)


def get_vision_embedder(settings) -> VisionEmbedder:
    """Factory: return the configured vision embedder instance.

    Raises ``ValueError`` for an unknown ``vision_provider`` so a
    misconfigured env var fails loud at first call rather than
    silently producing zero vectors.
    """
    provider = settings.vision_provider
    if provider == "doubao":
        return DoubaoVisionEmbedder(
            api_key=settings.doubao_vision_api_key,
            base_url=settings.doubao_vision_base_url,
            model=settings.doubao_vision_model,
        )
    if provider == "openai_clip":
        return OpenAIVisionEmbedder(
            api_key=settings.openai_clip_api_key,
            base_url=settings.openai_clip_base_url,
            model=settings.openai_clip_model,
        )
    if provider == "voyage":
        return VoyageVisionEmbedder(
            api_key=settings.voyage_api_key,
            model=settings.voyage_model,
        )
    raise ValueError(f"unknown vision_provider: {provider!r}")
```

- [ ] **Step 3: Verify imports + factory works**

Run:
```bash
cd apps/api && python -c "
from knowledge.multimodal.embedder import (
    OpenAIVisionEmbedder, VoyageVisionEmbedder, get_vision_embedder,
)
print('OpenAI dim:', OpenAIVisionEmbedder.dimension)
print('Voyage dim:', VoyageVisionEmbedder.dimension)
from core.config import get_settings
e = get_vision_embedder(get_settings())
print('factory returned:', type(e).__name__)
"
```
Expected:
```
OpenAI dim: 768
Voyage dim: 1024
factory returned: DoubaoVisionEmbedder
```

- [ ] **Step 4: Commit**

```bash
git add apps/api/src/knowledge/multimodal/embedder.py
git commit -m "feat(multimodal): OpenAIVisionEmbedder + VoyageVisionEmbedder + factory"
```

---

## Task 3: Replace `IMAGE_COLLECTION` constant with provider-aware function

**Files:**
- Modify: `apps/api/src/knowledge/startup.py:35-47, 86-149`

- [ ] **Step 1: Add the `get_image_collection_name` function**

After `DEFAULT_VISION_DIMENSION = 1024` (around line 47), add:

```python
# Stage 19 / M3 / tech-debt #19: per-provider collection names so each
# vision embedder writes to its own Qdrant collection (different
# dimensionalities cannot coexist in one collection). Legacy
# ``IMAGE_COLLECTION = "kb_image_vectors"`` is now an alias for the
# Doubao collection (see ``ensure_image_collection`` below).
_PROVIDER_COLLECTION_NAMES: dict[str, str] = {
    "doubao": "kb_image_vectors_doubao",
    "openai_clip": "kb_image_vectors_clip",
    "voyage": "kb_image_vectors_voyage",
}


def get_image_collection_name(provider: str) -> str:
    """Return the Qdrant collection name for the given vision provider.

    Raises ``ValueError`` for unknown providers — fail loud at boot
    rather than at the first upload.
    """
    try:
        return _PROVIDER_COLLECTION_NAMES[provider]
    except KeyError as exc:
        raise ValueError(
            f"unknown vision_provider: {provider!r}"
        ) from exc


# Backward-compat alias. New code MUST call get_image_collection_name.
# This constant points at the Doubao collection name (via the alias
# below) so legacy code that imports ``IMAGE_COLLECTION`` keeps working.
IMAGE_COLLECTION = "kb_image_vectors"
```

- [ ] **Step 2: Update `ensure_image_collection` to use the provider collection name + create alias**

Replace the entire `ensure_image_collection` function (lines 86–149) with:

```python
async def ensure_image_collection(
    qdrant_client: AsyncQdrantClient,
    *,
    dimension: int | None = None,
    provider: str | None = None,
) -> bool:
    """Ensure the vision-embedder Qdrant collection exists for the
    configured provider.

    Stage 19 / M3 / tech-debt #19: each provider writes to its own
    collection. The legacy ``kb_image_vectors`` name is created as an
    alias pointing at the provider's physical collection so existing
    data + tests that reference the old name continue to work.

    Returns ``True`` on success / already-exists, ``False`` on any
    failure. Never raises.
    """
    from core.config import get_settings

    if provider is None:
        provider = get_settings().vision_provider
    collection_name = get_image_collection_name(provider)
    if dimension is None:
        # Read the dimension from the embedder class — keeps this
        # function free of provider-specific imports beyond the
        # factory.
        from knowledge.multimodal.embedder import get_vision_embedder

        # Don't construct a real embedder here — we only need the
        # dimension. Use a cheap class lookup.
        dim_map = {"doubao": 1024, "openai_clip": 768, "voyage": 1024}
        dimension = dim_map[provider]

    try:
        if not await qdrant_client.collection_exists(collection_name):
            await qdrant_client.create_collection(
                collection_name=collection_name,
                vectors_config={"size": dimension, "distance": "Cosine"},
            )
            log.info(
                "qdrant.image_collection.created",
                collection=collection_name,
                provider=provider,
                vector_size=dimension,
            )
        else:
            log.info(
                "qdrant.image_collection.exists",
                collection=collection_name,
                provider=provider,
            )

        # Register the legacy alias → provider collection. Skip if
        # the alias name conflicts with a physical collection
        # (would happen on upgrades from M2.B where kb_image_vectors
        # was a real collection, not an alias — log WARNING so the
        # operator knows).
        try:
            aliases = await qdrant_client.get_collection_aliases(
                collection_name=collection_name
            )
            alias_names = {a.alias_name for a in aliases.aliases}
            if IMAGE_COLLECTION not in alias_names:
                from qdrant_client.http import models as qmodels
                await qdrant_client.update_collection_aliases(
                    changes=[
                        qmodels.AliasOperations(
                            create_alias=qmodels.CreateAlias(
                                alias_name=IMAGE_COLLECTION,
                                collection_name=collection_name,
                            )
                        )
                    ]
                )
                log.info(
                    "qdrant.image_collection.alias_created",
                    alias=IMAGE_COLLECTION,
                    target=collection_name,
                )
        except Exception as exc:
            log.warning(
                "qdrant.image_collection.alias_creation_failed",
                alias=IMAGE_COLLECTION,
                target=collection_name,
                error_type=type(exc).__name__,
            )

        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "already exists" in msg:
            log.info(
                "qdrant.image_collection.race_lost",
                collection=collection_name,
            )
            return True
        log.warning(
            "qdrant.image_collection.ensure_failed",
            collection=collection_name,
            vector_size=dimension,
            error_type=type(exc).__name__,
        )
        return False
```

- [ ] **Step 3: Verify imports + collection lookup**

Run:
```bash
cd apps/api && python -c "
from knowledge.startup import get_image_collection_name, IMAGE_COLLECTION
print('doubao:', get_image_collection_name('doubao'))
print('openai_clip:', get_image_collection_name('openai_clip'))
print('voyage:', get_image_collection_name('voyage'))
print('legacy alias:', IMAGE_COLLECTION)
"
```
Expected:
```
doubao: kb_image_vectors_doubao
openai_clip: kb_image_vectors_clip
voyage: kb_image_vectors_voyage
legacy alias: kb_image_vectors
```

- [ ] **Step 4: Commit**

```bash
git add apps/api/src/knowledge/startup.py
git commit -m "refactor(multimodal): per-provider collection names + Qdrant alias bootstrap"
```

---

## Task 4: Wire factory into the upload handler and retriever

**Files:**
- Modify: `apps/api/src/knowledge/multimodal/api.py:243-247` (factory instead of direct `DoubaoVisionEmbedder(...)`)
- Modify: `apps/api/src/knowledge/multimodal/api.py:327` (use `get_image_collection_name`)
- Modify: `apps/api/src/knowledge/multimodal/retriever.py:56-57` (factory + collection name)

- [ ] **Step 1: Update `api.py` upload handler**

Find:
```python
from knowledge.multimodal.embedder import DoubaoVisionEmbedder
```

Replace with:
```python
from knowledge.multimodal.embedder import get_vision_embedder
from knowledge.startup import get_image_collection_name
```

Find the embedder instantiation block (around line 243–247):
```python
    embedder = DoubaoVisionEmbedder(
        api_key=settings.doubao_vision_api_key,
        base_url=settings.doubao_vision_base_url,
        model=settings.doubao_vision_model,
    )
```

Replace with:
```python
    embedder = get_vision_embedder(settings)
    image_collection = get_image_collection_name(settings.vision_provider)
```

Find the Qdrant upsert call (around line 327):
```python
            await qdrant.upsert(
                collection_name="kb_image_vectors",
                points=points,
                wait=True,
            )
```

Replace `collection_name="kb_image_vectors"` with `collection_name=image_collection`.

- [ ] **Step 2: Update `retriever.py`**

Find the imports (around line 55–56):
```python
from knowledge.qdrant_client import DEFAULT_COLLECTION
from knowledge.startup import IMAGE_COLLECTION
```

Replace the second line with:
```python
from knowledge.startup import get_image_collection_name
from core.config import get_settings
```

Find every usage of `IMAGE_COLLECTION` in the file (the RRF query block). Replace each with:
```python
get_image_collection_name(get_settings().vision_provider)
```

(If there are multiple call sites, replace all. `IMAGE_COLLECTION` constant import is now unused — keep it for backward compat or delete the import; the spec said keep it as a deprecated alias.)

- [ ] **Step 3: Run the multimodal test tree**

Run:
```bash
cd apps/api && pytest tests/knowledge/multimodal/ -v
```
Expected: all pass. If a test breaks because it patched `DoubaoVisionEmbedder` directly, update the test to patch `get_vision_embedder` instead.

- [ ] **Step 4: Commit**

```bash
git add apps/api/src/knowledge/multimodal/api.py apps/api/src/knowledge/multimodal/retriever.py
git commit -m "refactor(multimodal): wire get_vision_embedder factory + per-provider collection"
```

---

## Task 5: Add unit tests for new adapters and factory

**Files:**
- Modify: `apps/api/tests/knowledge/multimodal/unit/test_vision_embedder.py` (append)

- [ ] **Step 1: Append the new test cases**

```python
from knowledge.multimodal.embedder import (
    OpenAIVisionEmbedder, VoyageVisionEmbedder, get_vision_embedder,
)
from core.config import Settings


@pytest.mark.asyncio
async def test_openai_clip_encode_returns_768_dim_vector():
    """OpenAI CLIP returns 768-dim vectors."""
    e = OpenAIVisionEmbedder(
        api_key="test-key",
        base_url="https://api.openai.com/v1",
        model="clip-vit-large-patch14",
    )
    try:
        with patch.object(e, "_post") as mock_post:
            mock_post.return_value = {
                "data": [{"embedding": [0.1] * 768}],
                "model": "clip-vit-large-patch14",
            }
            result = await e.encode(b"jpeg-bytes", mime_type="image/jpeg")
        assert len(result.vector) == 768
        assert result.model == "clip-vit-large-patch14"
    finally:
        await e.aclose()


@pytest.mark.asyncio
async def test_openai_clip_empty_api_key_returns_zero_vector():
    """Graceful degradation: no key → 768-dim zero vector."""
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
    e = VoyageVisionEmbedder(
        api_key="test-key",
        model="voyage-multimodal-3",
    )
    try:
        with patch.object(e, "_post") as mock_post:
            mock_post.return_value = {
                "data": [{"embedding": [0.1] * 1024}],
                "model": "voyage-multimodal-3",
            }
            result = await e.encode(b"png-bytes", mime_type="image/png")
        assert len(result.vector) == 1024
        assert result.model == "voyage-multimodal-3"
    finally:
        await e.aclose()


def test_voyage_encode_sends_inputs_shape():
    """Voyage uses 'inputs' (plural) with nested 'content' array."""
    import httpx

    captured: list[dict] = []

    async def fake_post(self_or_url, *args, **kwargs):
        # The signature varies — for unit tests it's easier to patch _post
        # and capture its payload argument.
        return {"data": [{"embedding": [0.0] * 1024}]}

    # Simpler: assert the payload shape by inspecting what encode() builds.
    e = VoyageVisionEmbedder(api_key="k", model="voyage-multimodal-3")

    async def capture(url_or_payload):
        if isinstance(url_or_payload, dict):
            captured.append(url_or_payload)
            return {"data": [{"embedding": [0.0] * 1024}]}

    # Patch _post to just capture + return success
    import knowledge.multimodal.embedder as emb_mod

    original_post = e._post
    e._post = lambda payload: capture(payload)
    import asyncio
    asyncio.run(e.encode(b"data", mime_type="image/png"))

    assert len(captured) == 1
    payload = captured[0]
    assert "inputs" in payload
    assert payload["inputs"][0]["content"][0]["type"] == "image_base64"
    assert payload["inputs"][0]["content"][0]["media_type"] == "image/png"


def test_factory_returns_doubao_by_default():
    """Default vision_provider is doubao."""
    settings = Settings()
    embedder = get_vision_embedder(settings)
    assert isinstance(embedder, DoubaoVisionEmbedder)


def test_factory_returns_openai_clip_for_clip_provider():
    settings = Settings(vision_provider="openai_clip", openai_clip_api_key="k")
    embedder = get_vision_embedder(settings)
    assert isinstance(embedder, OpenAIVisionEmbedder)


def test_factory_returns_voyage_for_voyage_provider():
    settings = Settings(vision_provider="voyage", voyage_api_key="k")
    embedder = get_vision_embedder(settings)
    assert isinstance(embedder, VoyageVisionEmbedder)


def test_factory_raises_on_unknown_provider():
    settings = Settings(vision_provider="bogus")  # type: ignore[arg-type]
    import pytest
    with pytest.raises(ValueError, match="unknown vision_provider"):
        get_vision_embedder(settings)


def test_adapter_dimensions_match_known_values():
    """Sanity: dimensions are the values the Qdrant collections will be sized with."""
    assert DoubaoVisionEmbedder.dimension == 1024
    assert OpenAIVisionEmbedder.dimension == 768
    assert VoyageVisionEmbedder.dimension == 1024
```

- [ ] **Step 2: Run the new tests**

Run:
```bash
cd apps/api && pytest tests/knowledge/multimodal/unit/test_vision_embedder.py -v
```
Expected: all (old + new) tests pass.

- [ ] **Step 3: Commit**

```bash
git add apps/api/tests/knowledge/multimodal/unit/test_vision_embedder.py
git commit -m "test(multimodal): OpenAI CLIP + Voyage adapter unit tests + factory tests"
```

---

## Task 6: Final verification + README update

- [ ] **Step 1: Run the full non-integration suite**

```bash
cd apps/api && pytest --collect-only -m "not integration" 2>&1 | tail -5
```
Expected: collection succeeds, 0 errors.

- [ ] **Step 2: Run multimodal test tree**

```bash
cd apps/api && pytest tests/knowledge/multimodal/ -v
```
Expected: all pass.

- [ ] **Step 3: Remove tech-debt #19 from README**

Find the tech-debt #19 entry and delete (or strike through). Confirm:
```bash
grep -n "tech-debt #19\|multi.vision\|multi.vision.*adapter" README.md
```
Expected: no match (or struck-through).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: remove tech-debt #19 entry (multi-vision adapter shipped)"
```

---

## Acceptance Checklist

- [ ] `vision_provider` env var selects among `doubao` / `openai_clip` / `voyage`.
- [ ] Switching provider writes to the matching per-provider collection name.
- [ ] `kb_image_vectors` remains a working alias to `kb_image_vectors_doubao`.
- [ ] All existing multimodal tests pass with default provider unchanged.
- [ ] New unit tests cover both adapters + factory function.
- [ ] OpenAI CLIP dimension = 768, Voyage dimension = 1024, Doubao dimension = 1024.
