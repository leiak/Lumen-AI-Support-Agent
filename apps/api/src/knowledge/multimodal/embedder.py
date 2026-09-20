"""Vision embedding abstraction + Doubao implementation.

Doubao embedding-vision API accepts image bytes (or base64-from-bytes)
via the OpenAI-compatible multimodal embeddings endpoint.

Ref: https://www.volcengine.com/docs/82379/1366569

Graceful degradation: if api_key is empty (dev without Doubao creds),
returns a zero vector and logs a warning. This lets the rest of the
pipeline (storage, retrieval) work without vision enabled.

PII contract: log lines MUST NOT include raw image bytes. Allowed
fields: ``model``, ``reason``, ``error_type``, ``mime_type``.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass
from typing import ClassVar

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    model: str


class VisionEmbedder:
    """Abstract interface — concrete implementations below."""
    dimension: ClassVar[int] = 1024

    async def encode(self, image_bytes: bytes, *, mime_type: str) -> EmbeddingResult:
        raise NotImplementedError


class DoubaoVisionEmbedder(VisionEmbedder):
    """Calls Doubao multimodal embeddings API via OpenAI-compatible shape.

    The :class:`httpx.AsyncClient` is long-lived (one per embedder
    instance) so the HTTP connection pool is reused across calls.
    Callers MUST ``await embedder.aclose()`` when done (or use the
    embedder as an async context manager).
    """

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
        """Close the underlying :class:`httpx.AsyncClient`."""
        await self._client.aclose()

    async def __aenter__(self) -> "DoubaoVisionEmbedder":
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
        # Graceful degradation: no key configured
        if not self._api_key:
            logger.warning(
                "vision.embedder.degraded",
                extra={"reason": "missing_api_key", "model": self._model},
            )
            return EmbeddingResult(vector=[0.0] * self.dimension, model=self._model)

        # Doubao accepts image_url with data URI
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

        # On exhaustion, return zero vector (caller decides if degraded is OK)
        logger.warning(
            "vision.embedder.failed",
            extra={
                "model": self._model,
                "error_type": type(last_exc).__name__ if last_exc else "unknown",
            },
        )
        return EmbeddingResult(vector=[0.0] * self.dimension, model=self._model)


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