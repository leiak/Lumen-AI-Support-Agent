"""Feishu webhook HTTP handlers."""
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from channel.feishu.signature import (
    decrypt_feishu_event,
    verify_feishu_signature,
    verify_timestamp_freshness,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/channel/feishu", tags=["channel-feishu"])

# Adapter wiring deferred to Task 4.13 (persistence layer).

# M1 stub key — Task 4.13 wires up real per-tenant credential lookup. Tests sign
# with this same stub key so signature verification passes during M1.
#
# WARNING: M1 deployments MUST NOT be public-internet-facing. The stub encrypt_key
# is a known constant; anyone can forge signatures against it. Restrict network
# exposure to a trusted test environment (e.g. local tunnel or internal staging)
# until Task 4.13 ships real per-tenant credential storage and lookup.
M1_STUB_ENCRYPT_KEY = "M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13"


@router.post("/webhook/{app_id}/verify")
async def url_verification(app_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Feishu URL verification handshake — echo the challenge back unchanged."""
    return {"challenge": payload.get("challenge", "")}


@router.post("/webhook/{app_id}")
async def receive_webhook(
    app_id: str,
    request: Request,
    x_lark_request_timestamp: Annotated[str | None, Header()] = None,
    x_lark_request_nonce: Annotated[str | None, Header()] = None,
    x_lark_signature: Annotated[str | None, Header()] = None,
) -> JSONResponse:
    """Receive a Feishu event.

    Verifies signature + timestamp freshness, decrypts if the event is
    AES-wrapped, and ACKs with 200. Parsing into a MessageEnvelope and
    persisting the binding is the caller's responsibility — this handler
    only handles the transport-level concerns.

    TODO(Task 4.13): resolve `app_id` -> Channel via the repository, look up
    the per-tenant encrypt_key from `credentials_encrypted`, and reject
    unknown apps with 404. For M1 we accept any `app_id` and use a stub
    encrypt_key — see `M1_STUB_ENCRYPT_KEY`.
    """
    body = await request.body()
    encrypt_key = M1_STUB_ENCRYPT_KEY

    ts = x_lark_request_timestamp or ""
    nonce = x_lark_request_nonce or ""
    sig = x_lark_signature or ""

    if not verify_timestamp_freshness(ts):
        logger.info(
            "feishu webhook: stale timestamp",
            extra={"app_id": app_id, "timestamp": ts},
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "timestamp too old or invalid"},
        )
    if not verify_feishu_signature(
        timestamp=ts,
        nonce=nonce,
        encrypt_key=encrypt_key,
        body=body,
        signature=sig,
    ):
        logger.info("feishu webhook: invalid signature", extra={"app_id": app_id})
        return JSONResponse(
            status_code=401, content={"detail": "invalid signature"}
        )

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400, content={"detail": "malformed JSON body"}
        )

    if isinstance(payload, dict) and "encrypt" in payload:
        try:
            decrypted = decrypt_feishu_event(
                encrypted=payload["encrypt"],
                iv=payload.get("iv", ""),
                encrypt_key=encrypt_key,
            )
            payload = json.loads(decrypted)
        except Exception:
            logger.exception(
                "feishu webhook: decrypt failed", extra={"app_id": app_id}
            )
            return JSONResponse(
                status_code=400, content={"detail": "decryption failed"}
            )

    # Defensive: some Feishu accounts echo URL verification through the main
    # webhook too — reply with the challenge in that case.
    if isinstance(payload, dict) and "challenge" in payload:
        return JSONResponse(
            status_code=200, content={"challenge": payload["challenge"]}
        )

    # For M1 we just ACK; persistence happens in Stage 5 (会话 + 消息).
    return JSONResponse(status_code=200, content={"ok": True})
