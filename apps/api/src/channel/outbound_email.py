"""SES SendEmail HTTP API wrapper.

We use the SES v2 SendEmail HTTP API directly (no boto3) to keep the
dependency surface small. The SigV4 signing is delegated to botocore's
auth module which is already present (boto3 transitive dep).

Ref: https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_SendEmail.html
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from botocore.auth import SigV4Auth  # type: ignore[import-untyped]
from botocore.awsrequest import AWSRequest  # type: ignore[import-untyped]
from botocore.credentials import Credentials  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


class EmailSendError(Exception):
    """Raised when SES SendEmail fails after retries.

    Attributes:
        status_code: HTTP status code if the failure was an HTTP response,
            None if it was a network-level error (timeout, connection refused).
        error_type: Bounded label for dashboards ('SES4xx' / 'SES5xx' /
            'timeout' / 'connect_error' / 'unknown'). Mirrors the
            `error_type` field in the corresponding log extra so callers
            and dashboards can correlate.
        attempts: Number of attempts made before giving up (for observability).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
        attempts: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.attempts = attempts


class EmailOutbound:
    """Sends plain-text reply emails via SES v2 SendEmail API."""

    def __init__(
        self,
        *,
        region: str,
        from_address: str,
        aws_access_key_id: str,
        aws_secret_access_key: str,
        timeout_seconds: float = 10.0,
        max_retries: int = 3,
    ) -> None:
        self._region = region
        self._from = from_address
        # botocore SigV4Auth expects an object with .access_key / .secret_key / .token
        self._creds = Credentials(aws_access_key_id, aws_secret_access_key)
        self._timeout = timeout_seconds
        self._max_retries = max_retries

    async def send_reply(
        self,
        *,
        tenant_id: str,
        to_email: str,
        subject: str,
        body_text: str,
        in_reply_to: str | None,
        references: list[str],
    ) -> str:
        """Returns SES MessageId. Raises EmailSendError on failure.

        Retries on 5xx (transient); does NOT retry on 4xx (permanent).
        """
        url = f"https://email.{self._region}.amazonaws.com/v2/email/outbound-emails"
        headers_list: list[dict[str, str]] = []
        if in_reply_to:
            headers_list.append({"Name": "In-Reply-To", "Value": in_reply_to})
        if references:
            headers_list.append({"Name": "References", "Value": " ".join(references)})

        payload: dict[str, Any] = {
            "FromEmailAddress": self._from,
            "Destination": {"ToAddresses": [to_email]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": body_text, "Charset": "UTF-8"}},
                    "Headers": headers_list,
                }
            },
            "EmailTags": [{"Name": "tenant_id", "Value": tenant_id}],
        }

        last_exc: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    req = AWSRequest(
                        method="POST",
                        url=url,
                        data=json.dumps(payload).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                    )
                    SigV4Auth(self._creds, "ses", self._region).add_auth(req)
                    prepared_headers = dict(req.headers.items())

                    resp = await client.post(
                        url,
                        content=req.body,
                        headers=prepared_headers,
                    )

                    if resp.status_code == 200:
                        body = resp.json()
                        return str(body["MessageId"])

                    # 4xx: don't retry (caller's fault or config issue)
                    if 400 <= resp.status_code < 500:
                        logger.warning(
                            "email.outbound.permanent_failure",
                            extra={
                                "tenant_id": tenant_id,
                                "status_code": resp.status_code,
                                "error_type": "SES4xx",
                            },
                        )
                        raise EmailSendError(
                            f"SES 4xx: {resp.status_code} {resp.text[:200]}",
                            status_code=resp.status_code,
                            error_type="SES4xx",
                            attempts=attempt + 1,
                        )

                    # 5xx: retry
                    last_exc = EmailSendError(
                        f"SES 5xx: {resp.status_code} {resp.text[:200]}",
                        status_code=resp.status_code,
                        error_type="SES5xx",
                        attempts=attempt + 1,
                    )
            except (httpx.HTTPError, asyncio.TimeoutError) as e:
                err_label = "timeout" if isinstance(e, asyncio.TimeoutError) else "connect_error"
                last_exc = EmailSendError(
                    f"SES network: {err_label}",
                    error_type=err_label,
                    attempts=attempt + 1,
                )

            # Exponential backoff (0.5s, 1s, 2s) — capped
            if attempt < self._max_retries - 1:
                await asyncio.sleep(0.5 * (2 ** attempt))

        # PII: no email address in logs (M1 discipline). tenant_id + error class
        # are sufficient for triage; the destination can be looked up via the
        # SES SendEmail MessageId in the SES suppression / bounce dashboard.
        final = EmailSendError(
            f"SES exhausted retries: {last_exc}",
            status_code=getattr(last_exc, "status_code", None),
            error_type=getattr(last_exc, "error_type", None) or "exhausted",
            attempts=self._max_retries,
        )
        logger.warning(
            "email.outbound.exhausted_retries",
            extra={
                "tenant_id": tenant_id,
                "error_type": final.error_type,
            },
        )
        raise final