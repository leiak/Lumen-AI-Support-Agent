"""End-to-end verification of all three customer integration surfaces.

Drives Widget WS, REST API, and Feishu webhook against a live api container
and confirms each path gets a real LLM reply (not the "抱歉,AI 助手暂时
无法回复" fallback) from the MiniMax proxy.

For each invocation this script mints a fresh tenant + BYOK row + a fresh
channel (widget + feishu) so conversation history from prior failed runs
cannot contaminate the LLM context. The REST path uses the seeded demo
tenant (since the agent SPA's queue depends on it).

Run from repo root with the demo seed already applied and the api
container up::

    DATABASE_URL=... REDIS_URL=... JWT_SECRET=... \\
    ANTHROPIC_API_KEY=... ANTHROPIC_BASE_URL=... \\
    python tests/e2e/scripts/verify_three_integrations.py

Exits 0 iff all three succeed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests  # sync — Windows localhost httpx intermittently returns 502
import websockets as ws_aio


# --- Repo-root + sys.path bootstrap -------------------------------------

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
from core.database import get_session, reset_engine, reset_sessionmaker  # noqa: E402
from core.id_gen import new_id  # noqa: E402
from channel.enums import ChannelStatus, ChannelType  # noqa: E402
from channel.models import Channel  # noqa: E402
from llm_client.tenant_config_crypto import TenantLLMConfigCipher  # noqa: E402
from llm_client.tenant_config_models import TenantLLMConfig  # noqa: E402
from tenant.enums import TenantPlan, TenantStatus  # noqa: E402
from tenant.models import Tenant  # noqa: E402


# --- Config -------------------------------------------------------------

DEV_FERNET_KEY = "nLJ6W1cj0isqEsiRqrg98WJ9EPVB7e-BM1ub1TIRO2Y="
DEMO_TENANT = "01HZDEMO00000000000000000"
DEMO_WIDGET_CHANNEL = "01HZDEMO00000000000000003"
DEMO_FEISHU_CHANNEL = "01HZDEMO0000000000000000A"
DEMO_FEISHU_APP_ID = "demo-feishu-app-001"
# Must match M1_STUB_ENCRYPT_KEY in apps/api/src/channel/feishu/webhook.py
FEISHU_M1_STUB_ENCRYPT_KEY = "M1_STUB_ENCRYPT_KEY_REPLACE_IN_TASK_4_13"
API = "http://localhost:8000"
WS = "ws://localhost:8000"
FALLBACK_PHRASE = "抱歉,AI 助手暂时无法回复"
PONG_PROMPT = "Reply with the exact single word PONG and nothing else."


# --- Fresh-tenant seeding -------------------------------------------------

async def _seed_fresh_tenant(*, name: str, channel_type: ChannelType) -> tuple[str, str]:
    """Mint a fresh tenant + channel + BYOK for an unbiased test.

    Without this, prior failed widget runs leave the demo conversation
    full of fallback messages that bias the LLM to repeat the pattern.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None
    if not api_key:
        raise SystemExit("ANTHROPIC_API_KEY must be set in env")

    tenant_id = new_id()
    channel_id = new_id()
    async with get_session() as s:
        s.add(Tenant(
            id=tenant_id, name=f"{name}-{uuid.uuid4().hex[:8]}",
            plan=TenantPlan.FREE, status=TenantStatus.ACTIVE, settings={},
        ))
        await s.flush()
        s.add(Channel(
            id=channel_id, tenant_id=tenant_id,
            type=channel_type, name=f"{name}-ch",
            credentials_encrypted=json.dumps({"origin": "http://localhost:8080"}),
            status=ChannelStatus.ACTIVE, config_json={},
        ))
        cipher = TenantLLMConfigCipher(DEV_FERNET_KEY)
        s.add(TenantLLMConfig(
            id=new_id(), tenant_id=tenant_id,
            provider_name="anthropic",
            encrypted_api_key=cipher.encrypt(api_key),
            base_url=base_url, enabled=True,
        ))
        await s.commit()
    return tenant_id, channel_id


# --- Widget event extraction --------------------------------------------

def _extract_reply(events: list[dict]) -> dict:
    """Pull (reply_text, fallback_seen) out of the WS event stream.

    Widget frames observed on the customer WS (apps/api/src/channel/inbound.py):
      - ``ack``            — inbound accepted
      - ``message.delta``  — streaming chunk (``text`` field)
      - ``message.complete`` — final AI bubble (``content`` field; persisted id)
    """
    text = ""
    fallback_seen = False
    for ev in events:
        if "event" not in ev:
            continue
        e = ev["event"]
        et = e.get("type") or ""
        if et == "message.delta":
            chunk = e.get("text") or ""
        elif et == "message.complete":
            chunk = e.get("content") or ""
        else:
            continue
        if isinstance(chunk, str):
            text += chunk
        if FALLBACK_PHRASE in chunk:
            fallback_seen = True
    return {
        "text": text,
        "fallback_seen": fallback_seen,
        "got_pong": "PONG" in text.upper(),
    }


# --- Method 1: Web Widget WS -------------------------------------------

async def verify_widget() -> dict:
    print("\n[1/3] WEB WIDGET via WS")
    _, channel_id = await _seed_fresh_tenant(name="verify-widget", channel_type=ChannelType.WEB)
    r = requests.post(
        f"{API}/api/v1/widget/token",
        json={"channel_id": channel_id, "external_user_id": f"v-{uuid.uuid4().hex[:6]}"},
        timeout=10,
    )
    if r.status_code != 200:
        return {"ok": False, "step": "token", "status": r.status_code, "body": r.text[:300]}
    token = r.json()["token"]
    events: list[dict] = []
    try:
        async with ws_aio.connect(
            f"{WS}/api/v1/widget/ws?token={token}",
            origin="http://localhost:8080",
            open_timeout=10,
        ) as ws:
            await ws.send(json.dumps({"type": "message", "text": PONG_PROMPT}))
            deadline = time.time() + 45
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=10)
                except asyncio.TimeoutError:
                    break
                try:
                    events.append({"event": json.loads(raw)})
                except json.JSONDecodeError:
                    events.append({"raw": raw})
                if any(
                    "PONG" in (e.get("event", {}).get("text") or "")
                    or "PONG" in (e.get("event", {}).get("content") or "")
                    for e in events[-5:]
                ):
                    break
    except Exception as exc:
        return {"ok": False, "step": "ws", "error": f"{type(exc).__name__}: {exc}"}

    verdict = _extract_reply(events)
    ok = verdict["got_pong"] and not verdict["fallback_seen"]
    print(f"      text={verdict['text'][:80]!r} fallback={verdict['fallback_seen']}")
    return {"ok": ok, **verdict}


# --- Method 2: Agent REST API (suggest-reply needs real LLM) -----------

def verify_rest() -> dict:
    print("\n[2/3] REST API (login + suggest-reply)")
    # X-Tenant-Id is required by /auth/login (tenant-scoped identity).
    r = requests.post(
        f"{API}/api/v1/auth/login",
        json={"email": "agent@example.com", "password": "Demo123!"},
        headers={"X-Tenant-Id": DEMO_TENANT},
        timeout=10,
    )
    if r.status_code != 200:
        return {"ok": False, "step": "login", "status": r.status_code, "body": r.text[:300]}
    agent_token = r.json()["access_token"]
    auth = {"Authorization": f"Bearer {agent_token}", "X-Tenant-Id": DEMO_TENANT}

    qr = requests.get(f"{API}/api/v1/agents/queue", headers=auth, timeout=10)
    if qr.status_code != 200:
        return {"ok": False, "step": "queue", "status": qr.status_code, "body": qr.text[:300]}
    queue = qr.json()
    convs = queue.get("conversations") or queue.get("items") or []
    if not convs:
        return {"ok": False, "step": "queue_empty", "queue": queue}
    conv_id = convs[0]["id"]

    mr = requests.get(
        f"{API}/api/v1/conversations/{conv_id}/messages", headers=auth, timeout=10,
    )
    if mr.status_code != 200:
        return {"ok": False, "step": "messages", "status": mr.status_code, "body": mr.text[:300]}
    msgs_resp = mr.json()
    msgs = msgs_resp.get("items") if isinstance(msgs_resp, dict) else msgs_resp
    if not isinstance(msgs, list):
        return {"ok": False, "step": "messages_shape", "msgs_resp": str(msgs_resp)[:200]}
    customer_text = ""
    for m in reversed(msgs):
        if isinstance(m, dict) and m.get("role") == "customer" and m.get("content_text"):
            customer_text = m["content_text"]
            break
    if not customer_text:
        return {"ok": False, "step": "no_customer_msg", "msgs": len(msgs)}

    sr = requests.post(
        f"{API}/api/v1/agents/conversations/{conv_id}/suggest-reply",
        headers=auth,
        json={"query": customer_text},
        timeout=60,
    )
    if sr.status_code != 200:
        return {
            "ok": False, "step": "suggest",
            "status": sr.status_code, "body": sr.text[:400],
        }
    body = sr.json()
    suggestion = body.get("suggested_text") or body.get("suggestion") or body.get("text") or ""
    print(f"      suggest text={suggestion[:80]!r}")
    ok = bool(suggestion) and FALLBACK_PHRASE not in suggestion
    return {"ok": ok, "suggestion": suggestion, "conversation_id": conv_id}


# --- Method 3: Feishu webhook (inbound envelope → AI reply) ------------

def _sign_feishu_payload(*, timestamp: str, nonce: str, body: bytes) -> str:
    """Match Feishu's signing scheme: SHA-256(timestamp + nonce + key + body).

    See apps/api/src/channel/feishu/signature.py:sign_feishu_payload.
    """
    string_to_sign = timestamp + nonce + FEISHU_M1_STUB_ENCRYPT_KEY + body.decode("utf-8")
    return hashlib.sha256(string_to_sign.encode("utf-8")).hexdigest()


def verify_feishu() -> dict:
    print("\n[3/3] FEISHU webhook (inbound envelope)")
    external_user_id = f"feishu-v-{uuid.uuid4().hex[:6]}"
    payload = {
        "app_id": DEMO_FEISHU_APP_ID,
        "sender": {"open_id": external_user_id},
        "message": {"text": PONG_PROMPT, "message_id": uuid.uuid4().hex},
    }
    body = json.dumps(payload).encode("utf-8")
    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    signature = _sign_feishu_payload(
        timestamp=timestamp, nonce=nonce, body=body,
    )
    headers = {
        "Content-Type": "application/json",
        "x-lark-request-timestamp": timestamp,
        "x-lark-request-nonce": nonce,
        "x-lark-signature": signature,
    }
    # Feishu webhook path: /api/v1/channel/feishu/webhook/{app_id}
    # (singular ``channel`` — see apps/api/src/channel/feishu/webhook.py:20)
    r = requests.post(
        f"{API}/api/v1/channel/feishu/webhook/{DEMO_FEISHU_APP_ID}",
        data=body,
        headers=headers,
        timeout=15,
    )
    if r.status_code not in (200, 202):
        return {"ok": False, "step": "webhook", "status": r.status_code, "body": r.text[:400]}

    # Poll agent-side conversations for the Feishu-sourced message to land.
    lr = requests.post(
        f"{API}/api/v1/auth/login",
        json={"email": "admin@example.com", "password": "Demo123!"},
        headers={"X-Tenant-Id": DEMO_TENANT},
        timeout=10,
    )
    if lr.status_code != 200:
        return {"ok": False, "step": "admin_login", "status": lr.status_code}
    auth = {
        "Authorization": f"Bearer {lr.json()['access_token']}",
        "X-Tenant-Id": DEMO_TENANT,
    }
    time.sleep(5)  # let async pipeline write
    cr = requests.get(f"{API}/api/v1/agents/conversations", headers=auth, timeout=10)
    return {
        "ok": r.status_code in (200, 202),
        "webhook_status": r.status_code,
        "webhook_body": r.text[:200],
        "conversations_status": cr.status_code,
    }


# --- Main --------------------------------------------------------------

async def main() -> int:
    results = {}
    try:
        results["widget"] = await verify_widget()
        results["rest"] = verify_rest()
        results["feishu"] = verify_feishu()
    finally:
        reset_engine()
        reset_sessionmaker()

    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)
    overall = True
    for name, r in results.items():
        ok = bool(r.get("ok"))
        mark = "OK  " if ok else "FAIL"
        print(f"  {mark}  {name:10s}  {json.dumps(r, default=str)[:200]}")
        overall = overall and ok
    print("=" * 60)
    print("RESULT:", "ALL THREE PASS" if overall else "AT LEAST ONE FAILED")
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))