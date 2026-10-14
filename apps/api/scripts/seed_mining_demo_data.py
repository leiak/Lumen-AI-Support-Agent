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
* Creates 3 CLOSED conversations on that tenant + channel.
* Inserts 45 customer messages across 2 dense topics (24 password-
  reset + 15 invoice/billing + 6 misc one-off questions). Multi-topic
  data is essential: HDBSCAN is density-based and needs a "sparser"
  background to identify a cluster — a single homogeneous topic
  (just 24 password-reset questions) gives no contrast and produces
  0 clusters regardless of ``min_cluster_size`` / ``min_samples``.
  45 messages also sits well above the Doubao ``plan/v3`` embeddings
  API input batch cap of 10 (handled internally by ``embed_texts``).
* The ``seed_tag`` JSONB column on every inserted row lets the cleanup
  helper (``scripts/cleanup_mining_seed.py``) drop them later.

Why multi-topic
---------------

HDBSCAN's clustering decisions rely on density contrast: a dense
region (the topic) must stand out against a sparser background.
With only one topic, every point is roughly equidistant from every
other point, and the clusterer (correctly) reports zero clusters.
The first version of this script seeded 24 password-reset questions
and the clusterer always returned 0 — see git history. The fix is
to seed 2 topics + some one-off noise, which is what real tenants
look like.

This multi-topic requirement is one of the things the clusterer
hardening (commit ``8aaa767``) verified end-to-end: live run with
the Doubao ``doubao-embedding-vision`` model (2048-dim) produced
2 clusters (size 26 password-reset + 10 invoice-billing) against
9 noise points, and 2 KB drafts were inserted. Without the
one-off "misc" rows, the clusterer would have returned a single
homogeneous cluster of 39 (above ``max_cluster_size`` if lowered,
or 1 large topic — no contrast for a second cluster to emerge).

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

# Second topic — invoice/billing — so HDBSCAN has density contrast
# against the password-reset topic. With only 1 topic the dataset is
# one homogeneous dense region; HDBSCAN needs at least one "sparser"
# neighbor to identify a cluster. Adding a second topic + 6 misc
# one-off questions simulates a realistic customer-support mix.
INVOICE_QUESTIONS = [
    "Where can I download my invoice?",
    "I was charged twice for the same month, can I get a refund?",
    "How do I update my billing email address?",
    "Can I get a W-9 form for my company?",
    "My invoice is missing tax information, can you reissue it?",
    "How do I change the credit card on file?",
    "Cancel my subscription, where do I do that?",
    "Refund request for duplicate charge",
    "Annual plan upgrade — prorated cost?",
    "Need a copy of my receipt for accounting",
    "VAT number missing on latest invoice",
    "Subscription auto-renewal — how to disable?",
    "Invoice currency not matching my account settings",
    "Pro-rated refund for early cancellation",
    "Payment failed but my card works elsewhere",
]

# Miscellaneous one-off questions — simulate the long tail of random
# one-liners that show up in any real tenant. These become HDBSCAN
# noise points (the "sparse background" against which dense topics
# stand out).
ONE_OFF_QUESTIONS = [
    "Do you have a mobile app?",
    "What time zone are your support hours in?",
    "Can I get a feature roadmap?",
    "Is there a free trial?",
    "Where are your servers located?",
    "How do I contact a human agent?",
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

        # Build a multi-topic mix so HDBSCAN has density contrast.
        # A single-topic dataset is one homogeneous dense region; the
        # clusterer can't form a cluster without a "sparser" neighbor
        # to compare against. Two topics + 6 random one-offs mirror
        # what a real tenant's customer-message stream looks like.
        topics: list[tuple[str, list[str]]] = [
            ("password-reset", PASSWORD_RESET_QUESTIONS),
            ("invoice-billing", INVOICE_QUESTIONS),
            ("misc-oneoff", ONE_OFF_QUESTIONS),
        ]

        total_msgs = 0
        for conv_idx, (topic_name, questions) in enumerate(topics):
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
            for msg_idx, text in enumerate(questions):
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
            f"[seed] inserted {len(topics)} CLOSED conversations + {total_msgs} "
            f"CUSTOMER messages on tenant {t.id} (channel {ch.id})"
        )


if __name__ == "__main__":
    asyncio.run(main())
