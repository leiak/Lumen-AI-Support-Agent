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

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbeddingResult:
    vector: list[float]
    model: str


class VisionEmbedder:
    """Abstract interface — concrete implementations below."""
    dimension: int = 1024

    async def encode(self, image_bytes: bytes, *, mime_type: str) -> EmbeddingResult:
        raise NotImplementedError


class DoubaoVisionEmbedder(VisionEmbedder):
    """Calls Doubao multimodal embeddings API via OpenAI-compatible shape."""

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

    async def _post(self, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
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
            except (httpx.HTTPStatusError, httpx.HTTPError) as e:
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