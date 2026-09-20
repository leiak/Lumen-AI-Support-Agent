# Tech Debt #19 — Multi-Vision Embedding Adapter

> **Status:** design approved (M3 kickoff). Implementation plan: `docs/superpowers/plans/2026-09-20-tech-debt-19-multi-vision-adapter.md` (TBD).

## Goal

Make the vision embedder provider-pluggable. Support **Doubao** (existing, default), **OpenAI CLIP ViT-L/14** (768-dim), and **Voyage Multimodal 3** (1024-dim) behind a single `VisionEmbedder` abstract base. The provider is selected at runtime via `settings.vision_provider`.

## Context

`apps/api/src/knowledge/multimodal/embedder.py` defines:
- `EmbeddingResult` dataclass (lines 28–31) — `vector: list[float]`, `model: str`
- `VisionEmbedder` ABC (lines 34–39) — `dimension: ClassVar[int] = 1024`, abstract `async def encode(image_bytes, *, mime_type) -> EmbeddingResult`
- `DoubaoVisionEmbedder` concrete impl (lines 42–124)

The adapter is instantiated in two call sites:
1. `apps/api/src/knowledge/multimodal/api.py:upload_multimodal` (lines 243–247) — hard-codes `DoubaoVisionEmbedder`
2. `apps/api/src/knowledge/multimodal/retriever.py` — same hard-code (need to verify exact line; explore agent flagged)

The Qdrant collection dimension is fixed at 1024 by `knowledge/startup.py:DEFAULT_VISION_DIMENSION`. Switching to a 768-dim provider (CLIP) requires either padding/trimming vectors or using a separate collection name.

### Key design constraint

**Anthropic Claude Vision is NOT an embedding model** — it returns text descriptions, not vectors. Unifying it under `VisionEmbedder` would require either (a) wrapping a separate `caption` path that writes to a parallel `kb_image_captions` collection, or (b) pretending "vector" = "embedding of the caption" which conflates two retrieval modes. This spec deliberately **excludes Claude Vision** — that's a separate spec (M3+ or M4) covering text descriptions of images, with a different Qdrant collection.

## Approach

**Per-provider Qdrant collection naming.** Each provider gets its own collection:
- `kb_image_vectors_doubao` (1024-dim) — Doubao, default
- `kb_image_vectors_clip` (768-dim) — OpenAI CLIP
- `kb_image_vectors_voyage` (1024-dim) — Voyage

The legacy `kb_image_vectors` collection name is kept as an alias for `kb_image_vectors_doubao` (via Qdrant collection alias) so existing data and tests continue to work.

### File-level changes

#### `apps/api/src/knowledge/multimodal/embedder.py`

Add:
1. `class OpenAIVisionEmbedder(VisionEmbedder)`:
   - `dimension: ClassVar[int] = 768`
   - Constructor: `api_key`, `base_url`, `model` (default `clip-vit-large-patch14`), `timeout_seconds`, `max_retries`
   - `encode(image_bytes, *, mime_type)` → POSTs `{base_url}/embeddings` with `Authorization: Bearer {api_key}`, body `{"model": self._model, "input": [{"type": "image_url", "image_url": {"url": data_uri}}]}`. Parses `data[0].embedding` as `vector`.

2. `class VoyageVisionEmbedder(VisionEmbedder)`:
   - `dimension: ClassVar[int] = 1024`
   - Constructor: `api_key`, `model` (default `voyage-multimodal-3`), `timeout_seconds`, `max_retries`
   - `encode(image_bytes, *, mime_type)` → POSTs `https://api.voyageai.com/v1/multimodalembeddings` with `Authorization: Bearer {api_key}`, body `{"model": self._model, "inputs": [{"content": {"type": "base64", "data": b64, "media_type": mime_type}}]}`. Parses `data[0].embedding` as `vector`.

3. `def get_vision_embedder(settings) -> VisionEmbedder`:
   ```python
   def get_vision_embedder(settings) -> VisionEmbedder:
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

4. Each new adapter gracefully degrades to the zero-vector fallback when `api_key` is empty (matches existing `DoubaoVisionEmbedder` behavior).

#### `apps/api/src/knowledge/startup.py`

1. Replace the single `IMAGE_COLLECTION = "kb_image_vectors"` constant with a `get_image_collection_name(provider: str) -> str` function:
   ```python
   def get_image_collection_name(provider: str) -> str:
       return {
           "doubao": "kb_image_vectors_doubao",
           "openai_clip": "kb_image_vectors_clip",
           "voyage": "kb_image_vectors_voyage",
       }[provider]
   ```
2. Keep `IMAGE_COLLECTION = "kb_image_vectors"` as a deprecated alias (logs a DeprecationWarning once at startup, points to `get_image_collection_name(settings.vision_provider)`).
3. `ensure_image_collection(qdrant_client)` takes the provider as a parameter; uses the matching dimension from `provider_class.dimension`.
4. On startup, create the alias `kb_image_vectors` → `kb_image_vectors_doubao` (so legacy code/tests still query by the old name).

#### `apps/api/src/knowledge/multimodal/api.py:upload_multimodal` (lines 243–247)

Replace direct `DoubaoVisionEmbedder(...)` instantiation with `embedder = get_vision_embedder(settings)`. The collection name passed to Qdrant upsert comes from `get_image_collection_name(settings.vision_provider)`.

#### `apps/api/src/knowledge/multimodal/retriever.py`

Same change: use `get_vision_embedder(settings)` and `get_image_collection_name(settings.vision_provider)`.

#### `apps/api/src/core/config.py`

Add to `Settings`:
```python
vision_provider: Literal["doubao", "openai_clip", "voyage"] = "doubao"
openai_clip_api_key: str = ""
openai_clip_base_url: str = "https://api.openai.com/v1"
openai_clip_model: str = "clip-vit-large-patch14"
voyage_api_key: str = ""
voyage_model: str = "voyage-multimodal-3"
```

### Test changes

#### `apps/api/tests/knowledge/multimodal/unit/test_vision_embedder.py`

For each new adapter (`OpenAIVisionEmbedder`, `VoyageVisionEmbedder`), add tests mirroring the existing Doubao ones:
- `test_encode_returns_correct_dimension` — `len(result.vector) == 768` (CLIP) / `1024` (Voyage)
- `test_encode_with_empty_api_key_returns_zero_vector` — graceful degradation
- `test_encode_retries_on_5xx` — `httpx.MockTransport` returns 500, 500, 200 → third call succeeds
- `test_encode_does_not_retry_on_4xx` — 401 returns `LLMError` immediately
- `test_encode_sends_correct_request_body` — capture the JSON body sent to the mock transport; assert payload shape

Plus:
- `test_get_vision_embedder_returns_correct_class` — for each provider
- `test_get_vision_embedder_raises_on_unknown_provider`

#### `apps/api/tests/knowledge/multimodal/integration/test_multimodal_e2e.py`

Add `test_provider_switch_changes_target_collection`:
1. Override `settings.vision_provider = "openai_clip"` via env or fixture
2. POST a PDF with an image
3. Mock `OpenAIVisionEmbedder.encode` to return a 768-dim vector
4. Assert Qdrant upsert target is `kb_image_vectors_clip` (NOT `kb_image_vectors_doubao`)
5. Assert upserted vector has `len == 768`

#### `apps/api/tests/knowledge/startup/test_collection_setup.py` (new or extend)

Add `test_image_collection_alias_created_for_doubao` — assert `kb_image_vectors` is an alias of `kb_image_vectors_doubao` after `ensure_image_collection` runs.

## Migration of existing data

The `kb_image_vectors` collection (created by M2.B) is renamed to `kb_image_vectors_doubao`. Two options:
- **(a) Qdrant collection rename** — Qdrant doesn't support rename; instead use snapshot + recreate + alias
- **(b) Drop and recreate** — destructive; only acceptable if M2.B image data is non-production

**Decision: (b) + log a WARNING at startup if `kb_image_vectors` exists without the alias.** New deployments use the alias; old deployments drop the legacy collection on first boot after upgrade. Image embeddings are recomputed on next PDF upload.

## PII discipline

No change. Image bytes are customer-uploaded content (PDF screenshots, KB illustrations) — not customer chat text. The existing rule "never log image bytes" already holds.

## Multi-tenant isolation

Each provider's collection carries `tenant_id` in payload, matching the existing `DoubaoVisionEmbedder` pattern. No change to retriever filters.

## Out of scope

- Anthropic Claude Vision (separate spec; requires parallel `kb_image_captions` collection)
- Cohere `embed-v3-image` (not requested; add later via same factory pattern)
- Local CLIP inference (ONNX runtime); all 3 adapters are remote HTTP

## Acceptance

- [ ] `vision_provider` env var selects among `doubao` / `openai_clip` / `voyage`.
- [ ] Switching provider writes to the matching per-provider collection name.
- [ ] `kb_image_vectors` remains a working alias to `kb_image_vectors_doubao`.
- [ ] All existing multimodal e2e tests pass with default provider unchanged.
- [ ] New unit tests cover both adapters + factory function.
