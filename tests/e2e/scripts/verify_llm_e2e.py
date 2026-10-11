"""Standalone LLM end-to-end smoke test.

Creates its own tenant + WEB channel + user (via direct SQLAlchemy
inserts so the FK graph stays valid), then drives the agent WebSocket
and verifies the AI reply is real (not the "AI 助手暂时无法回复"
fallback) by checking for either:

  - a non-fallback assistant_message text, OR
  - an ``llm_invoked`` event with a real provider name + model

Self-contained: does not depend on ``tests/e2e/scripts/seed.py`` being
left intact by other concurrent sessions. Each run mints fresh ULIDs
so there's no chance of colliding with a row wiped mid-run.

Run from anywhere; uses ``DATABASE_URL`` from the operator shell or
defaults to the deploy/ canonical localhost setup.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "apps" / "api" / "src").is_dir() and (
            candidate / "tests" / "e2e"
        ).is_dir():
            return candidate
    raise RuntimeError("could not locate repo root")


_API_SRC = _find_repo_root(Path(__file__)) / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))


# Late imports — must come after sys.path tweak above.
import httpx  # noqa: E402
import requests  # noqa: E402
import websockets  # noqa: E402

from core.database import get_session, reset_engine, reset_sessionmaker  # noqa: E402
from core.id_gen import new_id  # noqa: E402
from channel.enums import ChannelStatus, ChannelType  # noqa: E402
from channel.models import Channel  # noqa: E402
from tenant.enums import TenantPlan, TenantStatus  # noqa: E402
from tenant.models import Tenant  # noqa: E402
# ticket.models for FK resolution (Tenant.channel_id -> tickets -> conversations)
from ticket import models as _ticket_models  # noqa: E402,F401  -- FK resolution
from llm_client.tenant_config_crypto import TenantLLMConfigCipher  # noqa: E402
from llm_client.tenant_config_models import TenantLLMConfig  # noqa: E402

API_BASE = os.environ.get("API_BASE_URL", "http://localhost:8000")
WS_BASE = API_BASE.replace("http://", "ws://").replace("https://", "wss://")

FALLBACK_PHRASE = "AI 助手暂时无法回复"

# Dev Fernet key — matches docker-compose.yml's TENANT_LLM_FERNET_KEY
# and apps/example-integration/scripts/extend-seed.py. Local-only;
# prod must generate its own via Fernet.generate_key().
DEV_FERNET_KEY = "nLJ6W1cj0isqEsiRqrg98WJ9EPVB7e-BM1ub1TIRO2Y="


async def _seed_minimal_tenant() -> tuple[str, str]:
    """Insert tenant + WEB channel + BYOK row; return (tenant_id, channel_id).

    The BYOK row is the critical piece. The api container decrypts it
    using ``$TENANT_LLM_FERNET_KEY`` (which defaults to the same dev key
    as this script) so the agent graph gets a real resolver instead of
    the ``TenantLlmNotConfigured`` / ``ProviderUnavailable`` fallback.
    The widget token endpoint only needs the Channel to exist — no User
    row is required.
    """
    tenant_id = new_id()
    channel_id = new_id()

    api_key = (
        os.environ.get("ANTHROPIC_API_KEY", "")
        or os.environ.get("MINIMAX_API_KEY", "")
    ).strip()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None

    async with get_session() as s:
        s.add(
            Tenant(
                id=tenant_id,
                name=f"llm-smoke-{uuid.uuid4().hex[:8]}",
                plan=TenantPlan.FREE,
                status=TenantStatus.ACTIVE,
                settings={},
            )
        )
        await s.flush()  # ensure Tenant row exists before Channel FK insert
        s.add(
            Channel(
                id=channel_id,
                tenant_id=tenant_id,
                type=ChannelType.WEB,
                name="smoke-web",
                credentials_encrypted=json.dumps({"origin": "http://localhost:8080"}),
                status=ChannelStatus.ACTIVE,
                config_json={},
            )
        )
        if api_key:
            cipher = TenantLLMConfigCipher(DEV_FERNET_KEY)
            s.add(
                TenantLLMConfig(
                    id=new_id(),
                    tenant_id=tenant_id,
                    provider_name="anthropic",
                    encrypted_api_key=cipher.encrypt(api_key),
                    base_url=base_url,
                    enabled=True,
                )
            )
            print(f"[smoke] seeded BYOK row: provider=anthropic base_url={base_url}")
        else:
            print("[smoke] NO API KEY in env — BYOK row skipped (test will see fallback)")
        await s.commit()

    return tenant_id, channel_id


async def _issue_widget_token(channel_id: str) -> str:
    # httpx 0.x on Windows intermittently returns 502 with empty body
    # for localhost-only POSTs (probably keep-alive weirdness). The
    # standard library ``requests`` is more reliable here, so we use it
    # for the token call even though everything else is async.
    def _sync() -> str:
        r = requests.post(
            f"{API_BASE}/api/v1/widget/token",
            json={
                "channel_id": channel_id,
                "external_user_id": f"smoke-{uuid.uuid4().hex[:8]}",
            },
            timeout=10,
        )
        if r.status_code != 200:
            print(f"[smoke] widget/token FAIL {r.status_code}: {r.text[:300]}")
            print(f"[smoke] headers: {dict(r.headers)}")
        r.raise_for_status()
        return r.json()["token"]

    return await asyncio.to_thread(_sync)


async def _drive_widget(token: str) -> dict:
    """Open the widget WS, send one message, return captured events."""
    events: list[dict] = []
    async with websockets.connect(
        f"{WS_BASE}/api/v1/widget/ws?token={token}",
        open_timeout=10,
        # Widget WS rejects handshakes without an Origin matching
        # the channel's allowlist. Pin to the same origin the seed
        # channel row declares.
        origin="http://localhost:8080",
    ) as ws:
        events.append({"raw": await ws.recv()})
        # Widget WS frame schema (apps/api/src/widget/adapter.py):
        # type="message" + text="..." triggers process_inbound_envelope
        # which records the customer message and (via the agent graph)
        # streams an AI reply back through the connection manager.
        await ws.send(json.dumps({
            "type": "message",
            "text": "Reply with the exact single word PONG and nothing else.",
        }))
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
            except asyncio.TimeoutError:
                break
            try:
                events.append({"event": json.loads(raw)})
            except json.JSONDecodeError:
                events.append({"raw": raw})
    return {"events": events}


def _summarize(events: list[dict]) -> dict:
    """Pull the human-readable verdict out of the event stream."""
    texts: list[str] = []
    llm_invoked: dict | None = None
    fallback_seen = False
    for ev in events:
        if "event" not in ev:
            continue
        e = ev["event"]
        etype = e.get("type") or e.get("event") or ""
        if etype == "assistant_message":
            text = e.get("text") or e.get("content") or ""
            if text:
                texts.append(text)
            if FALLBACK_PHRASE in text:
                fallback_seen = True
        elif etype == "llm_invoked":
            llm_invoked = e
        elif etype == "agent_message_chunk":
            texts.append(e.get("delta") or e.get("text") or "")
    full = "".join(texts)
    return {
        "fallback_seen": fallback_seen,
        "llm_invoked": llm_invoked,
        "reply_text": full,
        "reply_contains_pong": "PONG" in full.upper(),
    }


async def main() -> int:
    print(f"[smoke] API = {API_BASE}")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MINIMAX_API_KEY")):
        print("[smoke] WARN: no ANTHROPIC_API_KEY/MINIMAX_API_KEY in env; BYOK seed was skipped")

    # 1) Minimal tenant + channel
    tenant_id, channel_id = await _seed_minimal_tenant()
    print(f"[smoke] seeded tenant={tenant_id} channel={channel_id}")

    # 2) Token + WS
    token = await _issue_widget_token(channel_id)
    print(f"[smoke] widget token issued (len={len(token)})")

    # 3) Drive
    captured = await _drive_widget(token)
    summary = _summarize(captured["events"])

    print("=" * 60)
    print("[smoke] reply text   :", repr(summary["reply_text"][:200]))
    print("[smoke] fallback seen:", summary["fallback_seen"])
    print("[smoke] llm_invoked  :", summary["llm_invoked"])
    print("[smoke] contains PONG:", summary["reply_contains_pong"])
    print("=" * 60)

    if summary["reply_contains_pong"] and not summary["fallback_seen"]:
        print("[smoke] ✅ REAL LLM REPLY via widget WS")
        return 0
    if summary["llm_invoked"]:
        print("[smoke] ⚠️  llm_invoked seen but reply was fallback or empty")
    print("[smoke] ❌ did NOT get a real LLM reply")
    return 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        reset_engine()
        reset_sessionmaker()
    sys.exit(rc)