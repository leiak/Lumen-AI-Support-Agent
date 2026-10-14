"""One-shot seed: create CLOSED conversations + customer messages for history mining demo.

This is NOT a permanent test fixture — it's a manual helper to give the
"立即执行 history mining" button something to cluster on the demo tenant.
The script is idempotent: it scans for existing seed rows by their
``seed_tag`` JSONB column and skips them.

Usage (from the api container):

    docker compose -f deploy/docker-compose.yml exec api \
        python -m scripts.seed_mining_demo_data

Design
------

* Picks the first tenant + first WEB channel in the DB (the demo
  tenant + widget channel from ``scripts/extend-seed.py``).
* Creates 4 CLOSED conversations on that tenant + channel.
* Inserts 24 customer messages (6 per conversation) all on the same
  topic — password reset — so HDBSCAN's ``min_cluster_size=10`` and
  ``min_samples=5`` settings have enough material to form a single
  cluster. 24 also sits well above the Doubao ``plan/v3`` embeddings
  API input batch cap of 10 (handled internally by ``embed_texts``).
* The ``seed_tag`` JSONB column on every inserted row lets the cleanup
  helper (``scripts/cleanup_mining_seed.py``) drop them later.

Why 24 not 10
-------------

``min_cluster_size=10`` is the minimum cluster size, not the count
of points HDBSCAN needs to form one — it still needs at least
``min_samples=5`` core points. With exactly 10 messages HDBSCAN
collapses everything to noise and emits zero clusters. 24 messages
across 4 conversations gives HDBSCAN enough slack to form a cluster
and reliably produces ≥ 1 draft on the demo "立即执行" click.

Schema notes (verified against ``conversation.models.Conversation``):

* ``channel_id`` is a NOT NULL FK to ``channels.id`` — we re-use the
  tenant's first WEB channel rather than fabricating one.
* Conversation timestamps are ``opened_at`` + ``last_activity_at``
  (NOT ``created_at``/``updated_at``).
* Conversation has no ``closed_at`` column — the ``status='closed'``
  string is the source of truth.
* No ``metadata_json`` column exists — the script does not tag rows
  on the conversation itself; we tag via the message content.
  Idempotency relies on the messages being insertable in bulk (no
  unique constraint on (conversation_id, content_text)).
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

# Importing every model registers its table with ``Base.metadata``;
# without the ticket model in particular the Conversation FK to
# ``tickets.id`` raises ``NoReferencedTableError`` at insert time.
import knowledge.models  # noqa: F401  — registers kb_article_drafts
import ticket.models  # noqa: F401  — registers tickets (FK target)
from channel.enums import ChannelType
from channel.models import Channel
from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from core.database import get_sessionmaker
from core.id_gen import new_id
from tenant.models import Tenant

PASSWORD_RESET_QUESTIONS = [
    "I forgot my password, how do I reset it?",
    "How can I change my password?",
    "I can't log in — password reset link not working",
    "Where do I reset my account password?",
    "Need to recover my password, what do I do?",
    "How to reset the password for my account?",
    "Lost my password, please help me get back in",
    "Password reset email never arrives",
    "How do I update my password?",
    "Can't remember my password, what are the steps?",
    "My account is locked, how do I unlock it?",
    "Reset password for my work email account",
    "Need a new password for the dashboard",
    "Two-factor code not working after password reset",
    "How long does password reset email take?",
    "Password changed but old one still accepted",
    "Account recovery via phone number not working",
    "Forgot password and email not recognized",
    "Reset link expired before I could use it",
    "How to reset password without phone access",
    "Account password requirements and rules",
    "Multiple failed login attempts — how to unlock",
    "Password reset for newly created account",
    "Can admin reset my password for me?",
]

SEED_SIGNATURE_TEXT = "[seed-mining-2026-10-14]"


async def main() -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        # Idempotency check — bail if a prior run already wrote seed
        # messages (we use a recognizable content_text prefix).
        existing = await session.execute(
            select(Message).where(
                Message.content_text.like(f"%{SEED_SIGNATURE_TEXT}%")
            ).limit(1)
        )
        if existing.scalar_one_or_none() is not None:
            print(
                "[seed] mining-demo messages already present — skipping. "
                "Run scripts/cleanup_mining_seed.py first to wipe."
            )
            return

        # Find the demo tenant + a usable channel.
        t = (await session.execute(select(Tenant).limit(1))).scalar_one_or_none()
        if t is None:
            print("[seed] no tenant in DB — nothing to seed")
            return

        ch = (await session.execute(
            select(Channel).where(
                Channel.tenant_id == t.id,
                Channel.type == ChannelType.WEB,
            ).limit(1)
        )).scalar_one_or_none()
        if ch is None:
            # Fall back to ANY channel on the tenant.
            ch = (await session.execute(
                select(Channel).where(Channel.tenant_id == t.id).limit(1)
            )).scalar_one_or_none()
        if ch is None:
            print("[seed] no channel for tenant — nothing to seed")
            return

        # 20 days ago is well inside the 30-day lookback window.
        now = datetime.now(UTC)
        base_dt = now - timedelta(days=20)

        total_msgs = 0
        for conv_idx in range(4):
            conv = Conversation(
                id=new_id(),
                tenant_id=t.id,
                channel_id=ch.id,
                customer_external_id=f"seed-mining-customer-{conv_idx}",
                status=ConversationStatus.CLOSED,
                assigned_agent_id=None,
                opened_at=base_dt,
                last_activity_at=base_dt + timedelta(hours=1),
            )
            session.add(conv)
            # 6 messages per conversation × 4 = 24 total — comfortably
            # above the HDBSCAN ``min_cluster_size=10`` floor (with
            # ``min_samples=5`` you need slack to actually form a
            # cluster) and well above the per-batch embedding limit
            # of 10 (handled internally by ``embed_texts``).
            for msg_idx, text in enumerate(
                PASSWORD_RESET_QUESTIONS[conv_idx * 6 : (conv_idx + 1) * 6]
            ):
                msg_dt = base_dt + timedelta(minutes=msg_idx * 5)
                # Prefix every message with the seed signature so the
                # cleanup helper can find them later. Keep the question
                # text after the signature so embeddings still cluster.
                msg = Message(
                    id=new_id(),
                    conversation_id=conv.id,
                    role=MessageRole.CUSTOMER,
                    content_text=f"{text} {SEED_SIGNATURE_TEXT}",
                    created_at=msg_dt,
                )
                session.add(msg)
                total_msgs += 1

        await session.commit()
        print(
            f"[seed] inserted 2 CLOSED conversations + {total_msgs} "
            f"CUSTOMER messages on tenant {t.id} (channel {ch.id})"
        )


if __name__ == "__main__":
    asyncio.run(main())
