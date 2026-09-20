"""Weekly Arq cron job — clusters past conversation questions → KB drafts.

Stage 18 / M2.B Task 8.

Pipeline
--------

1. Scan past ``history_mining_lookback_days`` of customer messages from
   ``CLOSED`` conversations (the natural "fully handled" state in the
   M1 conversation state machine — :class:`conversation.enums.ConversationStatus`).
2. Group by tenant — each tenant gets its own clusters so the
   embedding space and LLM context are never shared across tenants.
3. Per-tenant:
   a. Embed all questions via :func:`llm_client.embeddings.embed_texts`.
   b. Cluster via :class:`history_mining.clusterer.HdbscanClusterer`.
   c. For each cluster, pick the 5 representative questions nearest
      to the centroid and call :class:`history_mining.draft_generator.KBDraftGenerator`
      to produce a title / body / tags.
   d. Persist a :class:`knowledge.models.KbArticleDraft` row
      (``status='DRAFT'``). Admin review happens via
      ``/api/v1/admin/kb-drafts/{id}/{approve,reject}``.

PII discipline
--------------

Logs contain only opaque IDs (tenant_id ULID, cluster_id int, count,
error class). Raw question text, customer text, and exception ``repr``
are NEVER logged. The ``draft_generator`` module's LLM prompt contains
question text but is NOT logged.

Multi-tenant isolation
----------------------

The per-tenant loop isolates failures: one bad tenant (e.g. an
embedding-API key issue scoped to that tenant) does NOT poison the
rest of the run — the per-tenant block catches ``Exception`` and
logs ``history_mining.tenant_failed`` so the cron keeps moving.

Worker idempotency
------------------

The worker is the only writer of ``kb_article_drafts`` rows from this
pipeline, and there's no UNIQUE constraint on ``(tenant_id, cluster_id)``
because clusters are not stable across HDBSCAN runs (different
embedding model versions or random seeds can renumber them). The
admin flow handles duplicate APPROVED-→-APPROVED transitions via
the 409 Conflict branch on the existing status guard; reruns at
``history_mining_worker`` level will simply add MORE DRAFT rows for
the same cluster period — admins deduplicate at review time. This is
deliberate (a per-cluster UNIQUE would prevent legitimate "rerun with
newer embedding model" operations).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from arq import cron
from sqlalchemy import select

from conversation.enums import ConversationStatus, MessageRole
from conversation.models import Conversation, Message
from core.config import get_settings
from core.database import get_sessionmaker
from core.id_gen import new_id
from history_mining.clusterer import HdbscanClusterer
from history_mining.draft_generator import KBDraftGenerator
from knowledge.models import KbArticleDraft

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cron schedule constants
# ---------------------------------------------------------------------------

# arq cron convention: weekday=0 is Monday, weekday=6 is Sunday.
# We run Sunday 03:00 UTC to spread load away from the QA worker
# (which already uses minute=0 on its schedule).
WEEKDAY_SUNDAY_UTC = 6  # 0 = Monday ... 6 = Sunday
MIN_HOUR_UTC = 3
MIN_MINUTE_UTC = 0


async def history_mining_worker(ctx: dict[str, Any]) -> dict[str, int]:
    """Runs weekly (Sunday 03:00 UTC).

    Returns a summary dict ``{"processed": N, "drafts_created": M}``
    so the Arq job-result captures show useful operator metrics.

    Stages:

    1. Single session query: gather all (tenant_id, msg_id, text)
       tuples for customer messages from CLOSED conversations in
       the lookback window. ONE query — not per-tenant — because the
       WHERE clause already filters by status + role + created_at
       and Postgres handles the tenant grouping via the in-memory
       Python pass below.

    2. Group rows by tenant_id in Python (cheap; this is a small
       batch, not streaming).

    3. Per-tenant processing with try/except isolation.
    """
    settings = get_settings()
    lookback_days = settings.history_mining_lookback_days
    min_cluster_size = settings.history_mining_min_cluster_size
    max_cluster_size = settings.history_mining_max_cluster_size
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    sm = get_sessionmaker()
    async with sm() as session:
        result = await session.execute(
            select(
                Conversation.tenant_id,
                Message.id,
                Message.content_text,
            )
            .join(Message, Message.conversation_id == Conversation.id)
            .where(
                Conversation.status == ConversationStatus.CLOSED,
                Message.role == MessageRole.CUSTOMER,
                Message.created_at >= cutoff,
            )
        )
        rows = result.fetchall()
        if not rows:
            logger.info(
                "history_mining.no_data",
                extra={"lookback_days": lookback_days},
            )
            return {"processed": 0, "drafts_created": 0}

    # Group by tenant in Python.
    by_tenant: dict[str, list[tuple[str, str]]] = {}
    for tenant_id, msg_id, content_text in rows:
        by_tenant.setdefault(tenant_id, []).append((msg_id, content_text))

    drafts_total = 0
    processed_total = len(rows)

    for tenant_id, tenant_messages in by_tenant.items():
        try:
            drafts_n = await _process_tenant(
                tenant_id=tenant_id,
                messages=tenant_messages,
                min_cluster_size=min_cluster_size,
                max_cluster_size=max_cluster_size,
            )
            drafts_total += drafts_n
        except Exception as exc:  # noqa: BLE001 — per-tenant isolation
            # Per-tenant failure MUST NOT kill the rest of the run.
            logger.warning(
                "history_mining.tenant_failed",
                extra={
                    "tenant_id": tenant_id,
                    "error_type": type(exc).__name__,
                },
            )
            continue

    logger.info(
        "history_mining.run_complete",
        extra={
            "processed": processed_total,
            "drafts_created": drafts_total,
            "tenants_seen": len(by_tenant),
        },
    )
    return {"processed": processed_total, "drafts_created": drafts_total}


async def _process_tenant(
    *,
    tenant_id: str,
    messages: list[tuple[str, str]],  # (msg_id, content_text)
    min_cluster_size: int,
    max_cluster_size: int,
) -> int:
    """Cluster tenant's questions + generate drafts. Returns drafts created.

    Imported lazily: ``llm_client`` is a heavy dep (provider plugins,
    async client singletons) — keep it out of the module-import path
    so unit tests can spin up the worker without a real OpenAI key.
    """
    from llm_client.client import LLMClient
    from llm_client.embeddings import embed_texts

    # 1. Embed all questions. ``embed_texts`` returns ``EmbeddingResult``;
    # we only need ``.vectors``. Failures are logged + return 0 — the
    # tenant is retried next week.
    texts = [content for _, content in messages]
    try:
        emb_result = await embed_texts(texts=texts, tenant_id=tenant_id)
    except Exception as exc:  # noqa: BLE001 — graceful degradation
        logger.warning(
            "history_mining.embedding_failed",
            extra={
                "tenant_id": tenant_id,
                "error_type": type(exc).__name__,
                "n_messages": len(texts),
            },
        )
        return 0
    vectors = emb_result.vectors
    if not vectors:
        return 0

    vectors_array = np.array(vectors)

    # 2. Cluster.
    clusterer = HdbscanClusterer(
        min_cluster_size=min_cluster_size,
        max_cluster_size=max_cluster_size,
    )
    clusters = clusterer.cluster(vectors_array)
    if not clusters:
        logger.info(
            "history_mining.no_clusters",
            extra={
                "tenant_id": tenant_id,
                "n_messages": len(texts),
            },
        )
        return 0

    # 3. Per-cluster representative-question selection + draft generation.
    # ``LLMClient.with_config`` returns an LLMClient wired to the
    # configured (provider, model) pair. Operators can override the
    # mining LLM via ``HISTORY_MINING_PROVIDER`` / ``HISTORY_MINING_MODEL``
    # without touching the QA judge config (the previous behaviour
    # reused ``qa_judge_*`` settings by accident — operators tuning
    # QA judge would side-effect the mining pipeline). Empty override
    # values fall back to ``qa_judge_*`` for backward compatibility.
    settings = get_settings()
    mining_provider = settings.history_mining_provider or settings.qa_judge_provider
    mining_model = settings.history_mining_model or settings.qa_judge_model

    def _llm_factory() -> LLMClient:
        return LLMClient.with_config(
            provider=mining_provider,
            model=mining_model,
            tenant_id=tenant_id,
        )

    generator = KBDraftGenerator(llm_client_factory=_llm_factory)

    sm = get_sessionmaker()
    drafts_n = 0
    async with sm() as session:
        for cluster in clusters:
            cluster_indices = list(cluster.indices)
            cluster_vectors = vectors_array[cluster_indices]

            # Pick 5 representative questions nearest to centroid.
            # The cluster already has a centroid_idx (closest point
            # to the cluster mean) but we want the 5 closest, not
            # just the 1st.
            cluster_mean = cluster_vectors.mean(axis=0)
            distances = [
                (i, float(np.linalg.norm(v - cluster_mean)))
                for i, v in enumerate(cluster_vectors)
            ]
            distances.sort(key=lambda x: x[1])
            rep_local = [i for i, _ in distances[:5]]
            rep_global = [cluster_indices[i] for i in rep_local]
            rep_questions = [messages[i][1] for i in rep_global]
            rep_question_ids = [messages[i][0] for i in rep_global]

            # Generate draft (graceful fallback inside KBDraftGenerator)
            draft = await generator.generate(rep_questions)

            kb_draft = KbArticleDraft(
                id=new_id(),
                tenant_id=tenant_id,
                cluster_id=cluster.id,
                source_questions=rep_question_ids,
                suggested_title=draft.title,
                suggested_body=draft.body,
                suggested_tags=draft.suggested_tags,
                status="DRAFT",
            )
            session.add(kb_draft)
            drafts_n += 1

        await session.commit()

    logger.info(
        "history_mining.tenant_processed",
        extra={
            "tenant_id": tenant_id,
            "n_clusters": len(clusters),
            "n_drafts": drafts_n,
        },
    )
    return drafts_n


class WorkerSettings:
    """Arq ``WorkerSettings`` — exposes ``history_mining_worker`` as a cron.

    M2.A's ``qa.worker.WorkerSettings`` is the production entrypoint
    (registered via ``apps.api.src.workers``); this class is the
    ``history_mining`` module's own WorkerSettings and is mounted by
    importing it alongside the QA WorkerSettings if you run the
    modules separately.

    Cron schedule: Sunday 03:00 UTC (``weekday=6`` in arq — Monday=0
    so Sunday=6). The QA worker already uses minute={0}; running
    mining at 03:00 keeps the two crons from firing at the same
    minute and oversubscribing Redis.

    Functions list is empty because ``history_mining_worker`` is
    only invoked by the cron (no on-demand queue). Arq still
    requires the attribute; the empty list is correct.
    """

    functions: list = []
    cron_jobs = [
        cron(
            history_mining_worker,
            hour=MIN_HOUR_UTC,
            minute=MIN_MINUTE_UTC,
            weekday=WEEKDAY_SUNDAY_UTC,
        ),
    ]


__all__ = ["WorkerSettings", "history_mining_worker"]