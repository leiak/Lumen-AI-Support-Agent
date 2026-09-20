"""Tests for S3-compatible object store (M2.B / Stage 17)."""
import pytest
from unittest.mock import patch, MagicMock
from knowledge.multimodal.storage import S3ObjectStore, ObjectStoreError


@pytest.fixture
def store():
    return S3ObjectStore(
        endpoint="http://localhost:9000",
        bucket="test-bucket",
        access_key="ak",
        secret_key="sk",
        region="us-east-1",
    )


def test_put_uploads_to_s3_and_returns_url(store):
    """put() returns the S3 URL after successful upload."""
    with patch.object(store, "_client") as mock_client:
        store.put("kb/test.png", b"fake-image-bytes")
        mock_client.put_object.assert_called_once()
        call_kwargs = mock_client.put_object.call_args.kwargs
        assert call_kwargs["Bucket"] == "test-bucket"
        assert call_kwargs["Key"] == "kb/test.png"
        assert call_kwargs["Body"] == b"fake-image-bytes"


def test_get_downloads_bytes(store):
    with patch.object(store, "_client") as mock_client:
        mock_body = MagicMock()
        mock_body.read.return_value = b"downloaded"
        mock_client.get_object.return_value = {"Body": mock_body}

        result = store.get("kb/test.png")

        assert result == b"downloaded"
        mock_client.get_object.assert_called_once_with(
            Bucket="test-bucket", Key="kb/test.png"
        )


def test_botocore_client_uses_adaptive_retry_config(store):
    """Botocore must be configured for adaptive throttling retries.

    Defends against accidentally reverting to no-retry behavior; boto3's
    default is ``max_attempts=5 / standard`` mode — we want ``adaptive``
    so S3 ``SlowDown`` / ``RequestTimeout`` / ``ServiceUnavailable``
    errors are transparently retried by botocore itself (we do NOT add
    application-level retry on top — botocore handles the S3-specific
    retryable error classification, including which ones are retryable
    vs which require ``ThrottlingException`` subclass handling).

    Botocore normalizes ``max_attempts`` into ``total_max_attempts`` on
    the config dict; the exact mapping (``max_attempts=N`` →
    ``total_max_attempts=N`` or ``N+1`` depending on whether the initial
    call is counted) varies across botocore versions. We only assert
    the mode here + that total attempts >= 3 (the SLO we care about).
    """
    config = store._client.meta.config
    # botocore normalizes max_attempts → total_max_attempts
    assert config.retries["mode"] == "adaptive"
    assert config.retries["total_max_attempts"] >= 3


def test_put_propagates_client_error_without_app_retry(store):
    """``put()`` does NOT add application-level retry on top of botocore.

    Throttling is handled by botocore's adaptive retry layer (verified
    by ``test_botocore_client_uses_adaptive_retry_config``); if the
    caller patches ``put_object`` directly, the exception propagates
    after a single call — no second loop in our code.
    """
    from botocore.exceptions import ClientError

    with patch.object(store, "_client") as mock_client:
        mock_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "SlowDown", "Message": "throttled"}},
            "PutObject",
        )
        with pytest.raises(ClientError):
            store.put("key", b"data")
    assert mock_client.put_object.call_count == 1


def test_get_url_returns_cdn_style(store):
    url = store.get_url("kb/img.png")
    assert "test-bucket" in url
    assert "kb/img.png" in url


def test_get_url_for_real_s3_when_no_endpoint():
    """Real AWS S3 (no ``endpoint_url``) → virtual-hosted–style URL.

    Defends against the production crash where ``meta.endpoint_url`` is
    ``None`` (real AWS, no override) and ``rstrip("/")`` would raise
    ``AttributeError``. The fallback builds the canonical S3 URL.
    """
    store = S3ObjectStore(
        endpoint=None,  # No endpoint → real AWS S3
        bucket="prod-bucket",
        access_key="ak",
        secret_key="sk",
        region="us-west-2",
    )
    url = store.get_url("kb/img.png")
    assert url == "https://prod-bucket.s3.us-west-2.amazonaws.com/kb/img.png"
    assert "localhost" not in url