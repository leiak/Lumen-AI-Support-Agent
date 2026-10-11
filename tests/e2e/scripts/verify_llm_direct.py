"""Direct LLM client smoke test (no WS, no conversation).

Bypasses the agent graph + WebSocket and calls the LLM gateway directly.
Uses the dev Fernet key + the freshly-created tenant's BYOK row to
verify the full resolver pipeline: cipher decrypt -> AnthropicProvider
with custom base_url -> httpx POST to the MiniMax proxy.

Prints the raw response (or exception) so the operator can verify the
real LLM is reachable from the Lumen api process.
"""
from __future__ import annotations

import asyncio
import os
import sys
import traceback
import uuid
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

from core.database import get_session, reset_engine, reset_sessionmaker  # noqa: E402
from core.id_gen import new_id  # noqa: E402
from llm_client.tenant_config_crypto import TenantLLMConfigCipher  # noqa: E402
from llm_client.tenant_config_models import TenantLLMConfig  # noqa: E402
from llm_client.tenant_resolver import (  # noqa: E402
    TenantLLMConfigCache,
    build_tenant_resolver,
)
from llm_client.types import ChatMessage, ChatRequest, MessageRole  # noqa: E402
from tenant.enums import TenantPlan, TenantStatus  # noqa: E402
from tenant.models import Tenant  # noqa: E402

DEV_FERNET_KEY = "nLJ6W1cj0isqEsiRqrg98WJ9EPVB7e-BM1ub1TIRO2Y="


async def main() -> int:
    api_key = (
        os.environ.get("ANTHROPIC_API_KEY", "")
        or os.environ.get("MINIMAX_API_KEY", "")
    ).strip()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None

    if not api_key:
        print("[direct] NO ANTHROPIC_API_KEY in env -- nothing to verify")
        return 2

    tenant_id = new_id()
    async with get_session() as s:
        s.add(Tenant(
            id=tenant_id,
            name=f"llm-direct-{uuid.uuid4().hex[:8]}",
            plan=TenantPlan.FREE,
            status=TenantStatus.ACTIVE,
            settings={},
        ))
        await s.flush()
        cipher = TenantLLMConfigCipher(DEV_FERNET_KEY)
        s.add(TenantLLMConfig(
            id=new_id(),
            tenant_id=tenant_id,
            provider_name="anthropic",
            encrypted_api_key=cipher.encrypt(api_key),
            base_url=base_url,
            enabled=True,
        ))
        await s.commit()
    print(f"[direct] tenant={tenant_id} base_url={base_url}")

    cache = TenantLLMConfigCache(
        ttl_s=60, maxsize=16, cipher=cipher,
        settings=None,
    )
    resolver = await build_tenant_resolver(tenant_id, cache=cache)
    print(f"[direct] resolver built: {type(resolver).__name__}")

    request = ChatRequest(
        model="MiniMax-M3",
        messages=[ChatMessage(role=MessageRole.USER, content="Reply with the exact single word PONG and nothing else.")],
        max_tokens=20,
        temperature=0,
    )

    try:
        provider = resolver(request)
    except Exception as exc:
        print(f"[direct] resolver rejected model: {type(exc).__name__}: {exc}")
        return 1
    print(f"[direct] selected provider: {type(provider).__name__}")

    try:
        response = await provider.chat(request)
    except Exception as exc:
        traceback.print_exc()
        print(f"[direct] provider.chat raised: {type(exc).__name__}: {exc}")
        return 1

    print(f"[direct] response: {response!r}")
    text = ""
    if isinstance(getattr(response, "content", None), str):
        text = response.content
    if not text:
        for choice in getattr(response, "choices", []):
            msg = getattr(choice, "message", None)
            if msg and getattr(msg, "content", None):
                content = msg.content
                text += content if isinstance(content, str) else str(content)
    print(f"[direct] text: {text!r}")
    if "PONG" in text.upper():
        print("[direct] OK REAL LLM REPLY from MiniMax proxy")
        return 0
    print("[direct] FAIL did NOT see PONG in reply")
    return 1


if __name__ == "__main__":
    try:
        rc = asyncio.run(main())
    finally:
        reset_engine()
        reset_sessionmaker()
    sys.exit(rc)