"""Idempotent FEISHU channel + KB + 2 articles seed.

Mirrors the pattern of tests/e2e/scripts/seed.py (fixed ULIDs, async
session, skip-if-exists, reset_engine + reset_sessionmaker at the end).

3-step Article creation follows knowledge.models.Article docstring:
Article body lives on ArticleVersion.raw_text, and Article's
current_version_id FK is set in a 2nd UPDATE once the version row exists.

Anti-enumeration: this is a demo seed. The fixed ULIDs and FEISHU
credentials must not exist in production databases.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "apps" / "api" / "src").is_dir() and (
            candidate / "tests" / "e2e"
        ).is_dir():
            return candidate
    raise RuntimeError(
        f"could not locate repo root from {start} — "
        "expected to find apps/api/src and tests/e2e as siblings",
    )


_API_SRC = _find_repo_root(Path(__file__)) / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from channel.enums import ChannelStatus, ChannelType  # noqa: E402
from channel.models import Channel  # noqa: E402
from core.database import get_session, reset_engine, reset_sessionmaker  # noqa: E402
from core.id_gen import new_id  # noqa: E402
from knowledge.enums import ArticleStatus  # noqa: E402
# ticket.models MUST be imported before channel/knowledge models so that
# any cross-domain FKs (e.g. ticket.conversation_id -> conversations.id
# when present) resolve at ORM flush time. Importing only what we use
# (Channel, KnowledgeBase, Article) leaves SQLAlchemy unable to walk
# the FK graph and ``flush()`` raises NoReferencedTableError on the
# tenants.id target. tenant.models is also imported below for the same
# reason — Channel / KB / Article all FK to tenants.id.
from ticket import models as ticket_models  # noqa: E401,F401,E402  -- needed for FK resolution
from knowledge.models import Article, ArticleVersion, KnowledgeBase  # noqa: E402
from llm_client.tenant_config_crypto import TenantLLMConfigCipher  # noqa: E402
from llm_client.tenant_config_models import (  # noqa: E402
    TenantLLMConfigRepository,
)


# Deterministic IDs (26 chars, matches String(26) in models).
DEMO_TENANT_ID = "01HZDEMO00000000000000000"
FEISHU_APP_ID = "demo-feishu-app-001"
FEISHU_CHANNEL_ID = "01HZDEMO0000000000000000A"
DEMO_KB_ID = "01HZDEMO0000000000000000B"
DEMO_KB_SLUG = "demo-kb"
ARTICLE_RESET_ID = "01HZDEMO0000000000000000C"
ARTICLE_REFUND_ID = "01HZDEMO0000000000000000D"

# Demo tenant's LLM config: anthropic provider, routed through the
# Claude Code proxy when ANTHROPIC_BASE_URL is set. The plaintext API
# key is read from the host shell's ``$ANTHROPIC_API_KEY`` (set by the
# operator before running ``docker compose up``) and Fernet-encrypted
# with the same dev key the api container reads via
# ``$TENANT_LLM_FERNET_KEY``.
#
# Anti-enumeration: the dev Fernet key below is for local demos only —
# any real deployment must generate its own (see
# ``llm_client/tenant_config_crypto.py:6``).
DEMO_LLM_PROVIDER = "anthropic"
DEMO_TENANT_LLM_FERNET_KEY = "nLJ6W1cj0isqEsiRqrg98WJ9EPVB7e-BM1ub1TIRO2Y="


async def _seed_article(
    s, *, article_id: str, kb_id: str, title: str, body: str
) -> None:
    """3-step Article creation mirroring knowledge.models.Article.

    Body lives on ArticleVersion.raw_text; current_version_id FK is set
    in a 2nd UPDATE once the version row exists (column is NULL at
    insert time, so no deferral needed).
    """
    if await s.get(Article, article_id) is not None:
        return
    art = Article(
        id=article_id,
        tenant_id=DEMO_TENANT_ID,
        knowledge_base_id=kb_id,
        title=title,
        status=ArticleStatus.INDEXED,
        current_version_id=None,
    )
    s.add(art)
    await s.flush()

    version_id = new_id()
    content_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    s.add(
        ArticleVersion(
            id=version_id,
            article_id=article_id,
            version_number=1,
            raw_text=body,
            content_hash=content_hash,
        )
    )
    await s.flush()

    art.current_version_id = version_id
    await s.flush()


async def _seed_feishu_channel(s) -> None:
    if await s.get(Channel, FEISHU_CHANNEL_ID) is not None:
        return
    s.add(
        Channel(
            id=FEISHU_CHANNEL_ID,
            tenant_id=DEMO_TENANT_ID,
            type=ChannelType.FEISHU,
            name="demo-feishu",
            credentials_encrypted=json.dumps({"app_id": FEISHU_APP_ID}),
            status=ChannelStatus.ACTIVE,
            config_json={},
        )
    )
    await s.flush()


async def _seed_kb(s) -> None:
    """NOTE: KnowledgeBase has NO status field and KBStatus doesn't exist.

    Only ArticleStatus (DRAFT/INDEXING/INDEXED/FAILED) is defined in
    knowledge.enums.
    """
    if await s.get(KnowledgeBase, DEMO_KB_ID) is not None:
        return
    s.add(
        KnowledgeBase(
            id=DEMO_KB_ID,
            tenant_id=DEMO_TENANT_ID,
            slug=DEMO_KB_SLUG,
            name="Demo KB",
        )
    )
    await s.flush()


async def _seed_tenant_llm_config() -> None:
    """Upsert a tenant_llm_configs row for the demo tenant.

    The agent graph always goes through TenantResolver (M4.C strict
    mode), which requires a per-tenant enabled config + Fernet key.
    Without this row the agent refuses to invoke any LLM and falls
    back to the canned "AI 助手暂时无法回复" stub.

    Reads ``$ANTHROPIC_API_KEY`` from the shell (the operator's
    Claude Code key) and ``$ANTHROPIC_BASE_URL`` for the proxy URL
    (when set, threads to AnthropicProvider so the demo tenant routes
    through Claude Code's MiniMax proxy if the operator is using one).
    Both are optional: when ``ANTHROPIC_API_KEY`` is unset this is a
    no-op so the script remains safe to run with no credentials.

    Uses the dev Fernet key baked into DEMO_TENANT_LLM_FERNET_KEY —
    the api container reads the same key via ``$TENANT_LLM_FERNET_KEY``
    in docker-compose.yml. No real production deployment should ship
    this script; the dev key is for local demos only.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print(
            "  (skipping tenant_llm seed; $ANTHROPIC_API_KEY not set in env)"
        )
        return

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None
    cipher = TenantLLMConfigCipher(DEMO_TENANT_LLM_FERNET_KEY)
    encrypted = cipher.encrypt(api_key)

    # Repository uses its own sessionmaker internally so it doesn't
    # share a session with the seed-time inserts above; the BYOK row
    # is a single self-contained upsert.
    repo = TenantLLMConfigRepository()
    row = await repo.upsert(
        tenant_id=DEMO_TENANT_ID,
        provider_name=DEMO_LLM_PROVIDER,
        encrypted_api_key=encrypted,
        base_url=base_url,
        enabled=True,
    )
    print(
        f"  tenant_llm_config: tenant={DEMO_TENANT_ID} "
        f"provider={row.provider_name} base_url={row.base_url or '<api.anthropic.com>'}"
    )


async def main() -> None:
    try:
        async with get_session() as s:
            await _seed_feishu_channel(s)
            await _seed_kb(s)
            await _seed_article(
                s,
                article_id=ARTICLE_RESET_ID,
                kb_id=DEMO_KB_ID,
                title="How do I reset my password?",
                body=(
                    "To reset your password, open the login page and click "
                    "'Forgot password'. We will email you a secure reset link "
                    "valid for 30 minutes."
                ),
            )
            await _seed_article(
                s,
                article_id=ARTICLE_REFUND_ID,
                kb_id=DEMO_KB_ID,
                title="What is the refund policy?",
                body=(
                    "We offer a 30-day money-back guarantee on all plans. "
                    "Contact support with your order ID to initiate a refund."
                ),
            )
            await s.commit()
        # Tenant LLM config uses the Repository (its own sessionmaker)
        # so it can't share the session above.
        await _seed_tenant_llm_config()
    finally:
        # Mirror tests/e2e/scripts/seed.py: clear cached engine + sessionmaker
        # so the API process picks up a fresh event loop.
        reset_engine()
        reset_sessionmaker()

    print("extend-seed complete:")
    print(f"  tenant_id         = {DEMO_TENANT_ID}")
    print(f"  feishu_channel_id = {FEISHU_CHANNEL_ID}")
    print(f"  feishu_app_id     = {FEISHU_APP_ID}")
    print(f"  kb_id             = {DEMO_KB_ID}")
    print(f"  kb_slug           = {DEMO_KB_SLUG}")
    print(f"  article_ids       = [{ARTICLE_RESET_ID}, {ARTICLE_REFUND_ID}]")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        prog="extend-seed.py",
        description=(
            "Idempotent FEISHU channel + KB + 2 articles seed. Adds a "
            "feishu webhook channel, one demo KB (slug='demo-kb'), and "
            "two articles (password reset + refund policy) on top of "
            "the base seed."
        ),
        epilog=(
            "Example:\n"
            "  pnpm seed        # alias for `node --import tsx scripts/extend-seed.ts`\n"
            "  # or:\n"
            "  uv run python scripts/extend-seed.py\n"
            "\n"
            "Prereqs:\n"
            "  - tests/e2e/scripts/seed.py has already run\n"
            "  - alembic upgrade head applied (knowledge / channel configs)\n"
            "  - DATABASE_URL in apps/api/.env points at the demo DB\n"
            "\n"
            "Optional: ANTHROPIC_API_KEY in env enables the tenant_llm "
            "config row (without it, the demo tenant falls back to the "
            "canned 'AI 助手暂时无法回复' stub for chat)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.parse_args()
    try:
        asyncio.run(main())
    except RuntimeError as e:
        print(f"extend-seed.py failed: {e}", file=sys.stderr)
        sys.exit(2)
    except Exception as e:  # pragma: no cover — defensive
        print(f"extend-seed.py crashed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)
