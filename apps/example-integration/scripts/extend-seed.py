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
from knowledge.models import Article, ArticleVersion, KnowledgeBase  # noqa: E402


# Deterministic IDs (26 chars, matches String(26) in models).
DEMO_TENANT_ID = "01HZDEMO00000000000000000"
FEISHU_APP_ID = "demo-feishu-app-001"
FEISHU_CHANNEL_ID = "01HZDEMO0000000000000000A"
DEMO_KB_ID = "01HZDEMO0000000000000000B"
DEMO_KB_SLUG = "demo-kb"
ARTICLE_RESET_ID = "01HZDEMO0000000000000000C"
ARTICLE_REFUND_ID = "01HZDEMO0000000000000000D"


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
    asyncio.run(main())
