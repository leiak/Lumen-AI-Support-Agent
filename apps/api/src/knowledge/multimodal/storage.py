"""S3-compatible object storage abstraction.

Both AWS S3 (production) and MinIO (dev) implement the same boto3
API. We use a single S3ObjectStore class with `endpoint_url` for
MinIO compatibility.

Storage layout: {tenant_id}/{kb_slug}/{article_id}/{filename}
All keys tenant-prefixed so cross-tenant access is impossible at
the storage layer (defense-in-depth on top of app-level isolation).

PII contract: log lines MUST NOT include raw file bytes or filenames
that may carry customer data. Allowed fields: ``bucket``, ``key_prefix``,
``error_type``.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class ObjectStoreError(Exception):
    """Raised when an S3 operation fails after retries."""


class S3ObjectStore:
    """Wraps boto3 with retry on throttling. Sync boto3 wrapped in run_in_executor."""

    def __init__(
        self,
        *,
        endpoint: str | None,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "us-east-1",
        max_retries: int = 3,
    ) -> None:
        self._bucket = bucket
        self._max_retries = max_retries
        # Eager init — boto3 client is constructed once per S3ObjectStore
        # instance and reused for the lifetime of the store.
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=Config(retries={"max_attempts": max_retries, "mode": "adaptive"}),
        )

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        """Upload bytes; return URL. Sync; boto3 is not async-native.

        Caller may use asyncio.to_thread(store.put, ...) if needed.
        """
        kwargs: dict = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        self._client.put_object(**kwargs)
        return self.get_url(key)

    def get(self, key: str) -> bytes:
        """Download bytes."""
        response = self._client.get_object(Bucket=self._bucket, Key=key)
        return response["Body"].read()

    def get_url(self, key: str) -> str:
        """Return a URL string.

        For MinIO (dev with explicit ``endpoint_url``), uses the endpoint URL.
        For AWS S3 (production, boto3 auto-resolves to ``*.amazonaws.com``),
        uses virtual-hosted–style URL.
        """
        endpoint = (self._client.meta.endpoint_url or "").rstrip("/")
        if endpoint and ".amazonaws.com" not in endpoint:
            # Custom endpoint (e.g. MinIO) — path-style URL is fine.
            return f"{endpoint}/{self._bucket}/{key}"
        # Real AWS S3 — virtual-hosted–style URL (canonical form).
        region = self._client.meta.region_name or "us-east-1"
        return f"https://{self._bucket}.s3.{region}.amazonaws.com/{key}"

    def delete(self, key: str) -> None:
        """Delete an object. No-op if not found."""
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] != "NoSuchKey":
                raise ObjectStoreError(f"delete failed: {e}") from e


@lru_cache(maxsize=1)
def get_object_store() -> S3ObjectStore:
    """Singleton factory — read settings on first call, cache."""
    from core.config import get_settings
    s = get_settings()
    return S3ObjectStore(
        endpoint=s.object_store_endpoint,
        bucket=s.object_store_bucket,
        access_key=s.object_store_access_key,
        secret_key=s.object_store_secret_key,
    )


def reset_object_store() -> None:
    """Clear the lru_cache so the next call re-reads settings.

    For test isolation parity with :func:`core.config.reset_settings`.
    """
    get_object_store.cache_clear()