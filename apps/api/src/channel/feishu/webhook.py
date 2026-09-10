"""Feishu webhook HTTP handlers."""
import json
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from channel.feishu.adapter import FeishuAdapter
from channel.feishu.signature import (
    decrypt_feishu_event,
    verify_feishu_signature,
    verify_timestamp_freshness,
)
from channel.inbound import process_inbound_envelope
from channel.repository import ChannelRepository

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
    AES-wrapped, resolves the channel by ``app_id``, parses the event into
    a MessageEnvelope via the FeishuAdapter, and hands it off to the
    channel-agnostic inbound processor for conversation/message
    persistence.

    M1 limitation: signature verification uses ``M1_STUB_ENCRYPT_KEY``
    rather than the per-tenant key stored on the channel row — see
    ``M1_STUB_ENCRYPT_KEY`` for the security warning. The only protection
    against forged webhooks from an unrelated tenant is the 404-on-unknown
    app_id below; do not expose M1 deployments to the public Internet.
    """
    body = await request.body()
    # M1: signature verification uses the stub encrypt key — see warning
    # in M1_STUB_ENCRYPT_KEY. Task 4.13 wires up real per-tenant credential
    # lookup. Persistence is wired in Task 5.2 — see channel/inbound.py.
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

    # Resolve channel by app_id, parse envelope, persist via the
    # channel-agnostic inbound processor. process_inbound_envelope swallows
    # persistence errors and logs them, so we always ACK 200 to Feishu.
    channel_repo = ChannelRepository()
    channel = await channel_repo.get_by_app_id(app_id)
    if channel is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"unknown app_id {app_id}"},
        )

    adapter = FeishuAdapter()
    envelope = await adapter.parse_inbound(raw=payload, channel=channel)
    await process_inbound_envelope(envelope)
    return JSONResponse(status_code=200, content={"ok": True})
