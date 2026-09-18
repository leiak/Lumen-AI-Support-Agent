"""Offline RAG evaluation runner for the knowledge base (Task 6.14).

This module implements a self-contained eval that measures how well
``knowledge.retriever.retrieve_chunks`` (Task 6.11) finds the right
articles for a set of synthetic queries. The eval is the M1 milestone
marker for retrieval quality — it is what an operator runs when they
want a numeric answer to "is the knowledge base actually returning
relevant chunks?".

What it does
------------

1. Loads a JSON dataset (``rag_eval_set.json``) that contains:
   * A list of synthetic articles (the "knowledge base")
   * A list of synthetic queries, each with ``expected_relevant_article_ids``
     and ``expected_top_hit_position``
2. Provisions the dataset in the live DB: creates a tenant + KB + the
   listed articles, then runs ``knowledge.worker.index_article`` for
   each one so the Qdrant collection is populated.
3. For each query, calls ``retrieve_chunks`` and compares the top hits
   to the expected article set.
4. Computes four retrieval metrics:
   * ``hit_rate_at_1`` — fraction of queries whose top-1 hit is in
     the expected set.
   * ``hit_rate_at_5`` — fraction of queries with at least one expected
     article in the top-5.
   * ``mrr`` — Mean Reciprocal Rank (1/rank of the first relevant hit).
   * ``ndcg_at_5`` — Normalized Discounted Cumulative Gain at depth 5
     with binary relevance (1 if the article is expected, else 0).
5. *(Optional)* When invoked with ``--threshold-sweep``, additionally
   computes precision / recall across a grid of cosine-similarity
   thresholds (0.20 … 0.70 step 0.05) and recommends the lowest
   threshold that hits ``recall ≥ 0.9`` while keeping
   ``precision ≥ 0.5``. Stage 10.3 added this so an operator can
   calibrate ``RAGService.DEFAULT_SCORE_THRESHOLD`` against a real
   embedding model rather than the eyeballed 0.3 default.
6. Writes an ``EvalReport`` JSON to ``output_dir/eval_report.json`` and
   prints a human-readable summary to stdout.
7. Cleans up the tenant (cascade deletes articles, chunks, etc.).

Design choices
--------------

* **Stdlib only.** Metrics use plain arithmetic (no numpy/scipy) so the
  eval never depends on heavy numeric deps. The dataset is plain JSON
  so it round-trips through git cleanly.
* **Async all the way.** Every I/O step awaits; the entry point is
  ``async def``.
* **PII discipline.** The dataset is synthetic — no real customer
  data, no real company names. Logs carry only opaque IDs and counts.
* **Tenant isolation.** Cross-tenant queries in the dataset use a
  *different* tenant_id than the one the articles belong to. The
  retriever raises ``KnowledgeBaseNotFoundError`` for those — the
  runner treats that as a successful isolation (0 relevant hits).
* **Two embedding modes.** ``--real-embeddings`` uses the production
  ``embed_texts`` (which routes to Doubao / OpenAI per
  ``DEFAULT_EMBEDDING_MODEL``). The default mode is the hash-seeded
  mock so the suite can run offline. Real embeddings need the
  matching Qdrant collection dimensions — ``_ensure_collection_ready``
  reads ``get_settings().default_embedding_model`` and creates the
  collection at the right size if it doesn't already exist.

CLI
---

::

    # Mock embeddings (offline; framework-only assertion in CI)
    python -m tests.knowledge.eval.run_eval \\
        --dataset tests/knowledge/eval/rag_eval_set.json \\
        --output-dir ./eval_output

    # Real embeddings (Stage 10.3): calibrate recall + recommend threshold
    python -m tests.knowledge.eval.run_eval \\
        --dataset tests/knowledge/eval/rag_eval_set.json \\
        --output-dir ./eval_output_real \\
        --real-embeddings \\
        --threshold-sweep \\
        --top-k 10

Or programmatically::

    from tests.knowledge.eval.run_eval import run_eval
    report = await run_eval(dataset_path=..., output_dir=...)
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Make ``src`` importable when this module is executed directly. The
# pytest wrapper sets ``sys.path`` via ``conftest.py``; for the CLI
# path we add the ``src`` directory at runtime so the imports below
# resolve identically. The layout is:
#   apps/api/tests/knowledge/eval/run_eval.py
#   apps/api/src/
# so ``parents[3]`` lands on ``apps/api`` and the src dir is its
# ``src`` child. We also keep the conftest-injected path if present.
_SRC_DIR = Path(__file__).resolve().parents[3] / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from knowledge.enums import ArticleSourceType, ArticleStatus  # noqa: E402
from knowledge.models import (  # noqa: E402
    Article,
    ArticleVersion,
    KnowledgeBase,
)
from knowledge.qdrant_client import (  # noqa: E402
    DEFAULT_COLLECTION,
    DEFAULT_VECTOR_SIZE,
    ensure_collection,
)
from knowledge.repository import ArticleRepository, KnowledgeBaseRepository  # noqa: E402
from knowledge.retriever import (  # noqa: E402
    KnowledgeBaseNotFoundError,
    RetrievedChunk,
    retrieve_chunks,
)
from knowledge.worker import index_article  # noqa: E402
from tenant.enums import TenantPlan  # noqa: E402
from tenant.models import Tenant  # noqa: E402
from tenant.repository import TenantRepository  # noqa: E402

from core.database import get_session  # noqa: E402
from core.id_gen import new_id  # noqa: E402
from core.logging import get_logger  # noqa: E402

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class EvalReport:
    """The full output of a single ``run_eval`` invocation.

    Attributes
    ----------
    dataset_version:
        The ``version`` field of the loaded dataset (so a downstream
        consumer can correlate reports with the corpus that produced
        them).
    dataset_path:
        Absolute path to the dataset JSON that was scored.
    embedding_mode:
        ``"real"`` when ``--real-embeddings`` was passed (production
        embeddings via OpenAI/Doubao), ``"mock"`` when the hash-seeded
        stub was used. The report is honest about which backend ran
        so a downstream reader doesn't confuse the two.
    total_queries:
        Total number of queries evaluated (standard + edge cases).
    hit_rate_at_1:
        Fraction of queries whose top-1 retrieved article is in
        ``expected_relevant_article_ids``.
    hit_rate_at_5:
        Fraction of queries with at least one expected article in
        the top-5.
    mrr:
        Mean reciprocal rank across all queries. ``1/rank`` of the
        first relevant hit; ``0`` when no relevant hit in top-5.
    ndcg_at_5:
        Mean normalized DCG at depth 5 with binary relevance.
    per_query:
        List of per-query records with the predicted top-5 article
        ids, the first-hit rank, and a ``category`` field (standard,
        out_of_domain, cross_tenant).
    per_topic:
        ``{topic: {metric: value}}`` aggregates restricted to
        *standard* queries (edge cases are excluded from the topic
        rollups because they don't belong to a topic).
    threshold_sweep:
        When ``--threshold-sweep`` is on, a list of
        ``{threshold, precision, recall, f1, n_kept}`` rows scanned
        over a grid of cosine-similarity cutoffs, plus a
        ``recommended_threshold`` field on the recommended operating
        point. Empty list when the sweep wasn't requested.
    recommended_threshold:
        Convenience mirror of the recommended threshold from the
        sweep (``None`` when the sweep wasn't run or no candidate met
        the recall/precision target).
    elapsed_seconds:
        Wall-clock time for the whole eval run.
    """

    dataset_version: str
    dataset_path: str
    embedding_mode: str
    total_queries: int
    hit_rate_at_1: float
    hit_rate_at_5: float
    mrr: float
    ndcg_at_5: float
    per_query: list[dict] = field(default_factory=list)
    per_topic: dict[str, dict] = field(default_factory=dict)
    threshold_sweep: list[dict] = field(default_factory=list)
    recommended_threshold: float | None = None
    elapsed_seconds: float = 0.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_topic_vector(text: str, *, dim: int) -> list[float]:
    """Hash-seeded unit vector — same approach as ``test_retriever.py``.

    The retriever's mock vector must be deterministic across calls so
    that a query's embedding and an article's embedding yield a
    stable cosine similarity. The eval runner uses the same hashing
    convention the integration tests use, so anyone reading the test
    file finds a familiar function.

    ``dim`` is REQUIRED (no default) — it must match the
    ``article_chunks`` collection's configured vector dimension,
    otherwise Qdrant rejects the upserts with a 400 ``Bad Request``.
    Callers in this module always read the dim from
    ``vector_size_for_model(get_settings().default_embedding_model)``
    so the mock stays in lockstep with whatever model the live
    pipeline would use.

    With a real OpenAI / Doubao client (``--real-embeddings``), this
    stub is NOT used — the live ``embed_texts`` runs instead.
    """
    seed = hashlib.sha256(text.encode("utf-8")).digest()
    expanded = (seed * ((dim // len(seed)) + 1))[:dim]
    raw = [(b / 127.5) - 1.0 for b in expanded]
    norm_sq = sum(x * x for x in raw) or 1.0
    norm = norm_sq ** 0.5
    return [x / norm for x in raw]


def _configured_vector_size() -> int:
    """Return the dim for ``article_chunks`` based on the live settings.

    Centralizes the "what dim should the eval use" question so the
    mock, the Qdrant collection, and the real embedding backend all
    agree. Reads ``DEFAULT_EMBEDDING_MODEL`` from settings so the
    mock matches the live collection dim (e.g. 2048 for
    ``doubao-embedding-vision``); falls back to
    ``DEFAULT_VECTOR_SIZE = 1536`` only when the model is unknown
    (which raises ``ValueError`` in production code too).
    """
    from core.config import get_settings
    from knowledge.qdrant_client import vector_size_for_model

    return vector_size_for_model(get_settings().default_embedding_model)


def _patch_embed_topic_coded(*, dim: int) -> None:
    """Patch ``embed_texts`` to return hash-seeded vectors of the given dim.

    The worker imports ``embed_texts`` at module-load time; the
    retriever does too. We patch both symbols so the eval is
    independent of network access. Tests can call
    :func:`_patch_embed_topic_coded` once at the top of the runner.

    ``dim`` MUST equal the ``article_chunks`` collection's vector
    size — otherwise the mock-emitted vectors get rejected at
    upsert time with a 400 Bad Request. Pass
    ``_configured_vector_size()`` to stay in lockstep with whatever
    the live model would produce.

    NOTE: this is a one-way patch by default. Real-embedding mode
    needs to *restore* the originals first via
    :func:`_restore_embed_originals` — otherwise the module-load
    patch leaks into the real run and you get mock-grade metrics.
    """
    from knowledge import retriever as retriever_module
    from knowledge import worker as worker_module

    class _Stub:
        def __init__(self, vectors: list[list[float]]) -> None:
            self.vectors = vectors
            self.model = "text-embedding-3-small"
            self.usage = type("U", (), {"prompt_tokens": 0, "total_tokens": 0})()

    async def _stub(*, texts, model, tenant_id=None, client=None):
        return _Stub([_make_topic_vector(t, dim=dim) for t in texts])

    worker_module.embed_texts = _stub  # type: ignore[assignment]
    retriever_module.embed_texts = _stub  # type: ignore[assignment]


def _restore_embed_originals() -> bool:
    """Restore the unpatched ``embed_texts`` on the worker + retriever.

    Called at the top of ``run_eval`` when ``--real-embeddings`` is
    requested. The module-load :func:`_patch_embed_topic_coded` call
    runs unconditionally for the offline / CI path; without this
    restore the real mode would silently inherit the hash-seeded stub
    and report mock-grade metrics while logging ``embedding_mode=real``.

    We cache the originals once via the worker-module sentinel
    ``_rag_eval_original_embed_texts`` so a second ``run_eval`` call
    in the same process doesn't re-bind to the stub (which would
    replace a real ``embed_texts`` with a copy of itself).

    Returns ``True`` if the originals were restored, ``False`` if
    there was nothing to restore (e.g. first call in this process).
    """
    from knowledge import retriever as retriever_module
    from knowledge import worker as worker_module

    # Cache the originals the first time we restore.
    cached = getattr(worker_module, "_rag_eval_original_embed_texts", None)
    if cached is None:
        cached = worker_module.embed_texts
        worker_module._rag_eval_original_embed_texts = cached  # type: ignore[attr-defined]

    worker_module.embed_texts = cached  # type: ignore[assignment]
    retriever_module.embed_texts = cached  # type: ignore[assignment]
    return True


def _dcg_at_k(relevances: list[int], k: int) -> float:
    """Discounted Cumulative Gain at depth ``k`` with binary relevance.

    DCG = sum_{i=1..k} rel_i / log2(i+1).  The log base 2 discounts
    deeper hits, mirroring the standard IR definition.
    """
    score = 0.0
    for i, rel in enumerate(relevances[:k], start=1):
        if rel:
            score += 1.0 / _log2(i + 1)
    return score


def _log2(x: int) -> float:
    """log2 via ``math.log`` — kept as a tiny helper to avoid an
    unconditional ``import math`` at module top.
    """
    import math

    return math.log2(x)


def _ndcg_at_k(relevances: list[int], k: int) -> float:
    """nDCG@k with binary relevance, returning 0.0 when ideal is 0.

    ``relevances`` is the predicted list in rank order. The ideal
    ranking sorts relevance descending; when the ideal DCG is 0
    (no relevant items in the prediction at all) we return 0.0 —
    nDCG is undefined in that case and a value of 0 matches the
    "no signal" reading the metrics should report.
    """
    ideal = sorted(relevances, reverse=True)
    idcg = _dcg_at_k(ideal, k)
    if idcg == 0.0:
        return 0.0
    return _dcg_at_k(relevances, k) / idcg


# ---------------------------------------------------------------------------
# Data provisioning
# ---------------------------------------------------------------------------


async def _ensure_collection_ready(*, vector_size: int | None = None) -> None:
    """Idempotently create the Qdrant collection if missing.

    Mirrors the production startup path — the worker assumes the
    collection exists, so the eval must create it before indexing.

    ``vector_size`` is the *target* size for the collection. When the
    eval runs in ``--real-embeddings`` mode the caller passes the
    configured model's dimension (via ``vector_size_for_model(get_settings().default_embedding_model)``)
    so a freshly-created collection matches the vectors the live
    ``embed_texts`` will produce. The mock mode keeps the historical
    ``DEFAULT_VECTOR_SIZE = 1536`` fallback so legacy fixtures
    continue to work.

    The helper is safe to call against a collection that already
    exists at a *different* size: ``ensure_collection`` returns
    ``True`` on the probe without touching it, and the subsequent
    upsert call will fail loudly at Qdrant if the dimension mismatches
    the live embedding backend. That's the behavior we want — a
    silent truncation would be much worse.
    """
    if vector_size is None:
        vector_size = DEFAULT_VECTOR_SIZE
    ok = await ensure_collection(
        name=DEFAULT_COLLECTION,
        vector_size=vector_size,
    )
    if not ok:
        raise RuntimeError(
            "ensure_collection returned False — Qdrant is unreachable. "
            "Cannot run RAG eval."
        )


async def _create_tenant(*, name: str) -> Tenant:
    """Insert a fresh Tenant row. Returns the persisted tenant."""
    return await TenantRepository().create(name=name, plan=TenantPlan.FREE)


async def _create_kb(
    *,
    tenant_id: str,
    name: str,
    slug: str,
    embedding_model: str,
    chunk_size: int,
    chunk_overlap: int,
) -> KnowledgeBase:
    """Insert a KnowledgeBase row with the full set of M1 parameters.

    The eval always uses a fresh tenant per run so the
    ``uq_knowledge_bases_tenant_slug`` UNIQUE constraint cannot
    trip. Returns the persisted ``KnowledgeBase`` row with ``id``
    populated.
    """
    return await KnowledgeBaseRepository().create(
        tenant_id=tenant_id,
        name=name,
        slug=slug,
        description=None,
        embedding_model=embedding_model,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


async def _create_article_with_version(
    *,
    tenant_id: str,
    knowledge_base_id: str,
    title: str,
    raw_text: str,
) -> tuple[Article, ArticleVersion]:
    """Create Article + v1 ArticleVersion in a single transaction.

    Uses :class:`ArticleRepository.create` which wires the
    ``current_version_id`` for us. The worker can then run with no
    manual FK plumbing.
    """
    return await ArticleRepository().create(
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        title=title,
        source_type=ArticleSourceType.MANUAL,
        raw_text=raw_text,
    )


async def _delete_tenant(tenant_id: str) -> None:
    """Best-effort tenant delete — cascades to KBs, articles, chunks.

    The retriever's Qdrant points are NOT deleted here; the eval
    cleans them up explicitly via ``_cleanup_qdrant_for_tenant``
    *before* the cascade so the cascade can drop the FK rows safely
    (and so we don't leak Qdrant vectors between runs).
    """
    async with get_session() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is not None:
            await session.delete(tenant)
            await session.commit()


async def _cleanup_qdrant_for_tenant(*, tenant_id: str) -> int:
    """Sweep every Qdrant point that carries ``tenant_id`` in its payload.

    The retriever cleanup path (``delete_article_vectors``) is per
    article; for the eval we want a tenant-wide sweep, so we use
    a payload filter and let Qdrant batch the delete.
    """
    from core.qdrant import get_qdrant_client
    from qdrant_client.http import models as qmodels

    client = get_qdrant_client()
    flt = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="tenant_id",
                match=qmodels.MatchValue(value=tenant_id),
            )
        ]
    )
    try:
        await client.delete(
            collection_name=DEFAULT_COLLECTION,
            points_selector=flt,
            wait=True,
        )
        return 0
    except Exception as exc:
        log.warning(
            "rag_eval.qdrant_cleanup_failed",
            tenant_id=tenant_id,
            error_type=type(exc).__name__,
        )
        return -1


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


async def run_eval(
    *,
    dataset_path: str,
    output_dir: str,
    use_real_embeddings: bool = False,
    do_threshold_sweep: bool = False,
    top_k: int = 5,
) -> EvalReport:
    """Run the full offline RAG eval against the loaded dataset.

    Parameters
    ----------
    dataset_path:
        Absolute or relative path to a JSON file matching the schema
        described in ``rag_eval_set.json``.
    output_dir:
        Directory to write ``eval_report.json`` into. Created if
        missing.
    use_real_embeddings:
        When ``True`` the production ``embed_texts`` (which routes
        through Doubao / OpenAI per ``DEFAULT_EMBEDDING_MODEL``) is
        used instead of the hash-seeded mock. The Qdrant collection
        is ensured at the live model's dimension so the upserts
        succeed. Default ``False`` keeps the mock mode for offline /
        CI runs that don't have API keys.
    do_threshold_sweep:
        When ``True`` the report additionally carries a
        ``threshold_sweep`` table plus a ``recommended_threshold``
        calibrated for ``recall ≥ 0.9`` and ``precision ≥ 0.5``.
        Implies ``top_k >= 10`` — the sweep walks the score
        distribution across more than the standard 5 hits, so
        callers that pass this flag should also pass
        ``top_k=10`` (or higher) for meaningful results.
    top_k:
        Number of hits ``retrieve_chunks`` should return per query.
        Default ``5`` matches the production default; ``10`` is the
        recommended value when running the threshold sweep.

    Returns
    -------
    EvalReport
        Full per-query + per-topic metrics. Also persisted to
        ``output_dir/eval_report.json`` and summarized to stdout.

    Raises
    ------
    FileNotFoundError:
        ``dataset_path`` does not exist.
    ValueError:
        The dataset is malformed (missing required keys, mismatched
        ids, etc.).
    RuntimeError:
        The Qdrant collection could not be ensured (so the indexing
        step would fail). The eval aborts before any DB writes.
    """
    started = time.perf_counter()

    # ---- 1. Load dataset ------------------------------------------------
    dataset_file = Path(dataset_path)
    if not dataset_file.exists():
        raise FileNotFoundError(f"eval dataset not found: {dataset_path}")
    dataset = json.loads(dataset_file.read_text(encoding="utf-8"))
    _validate_dataset(dataset)

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)

    # ---- 2. Resolve embedding backend ---------------------------------
    # Both modes use the *configured* model's vector dim so the mock
    # stays in lockstep with the Qdrant collection. Real-embedding
    # mode additionally skips the mock patch so ``embed_texts`` hits
    # the live AsyncOpenAI singleton (Doubao / OpenAI).
    from core.config import get_settings

    configured_model = get_settings().default_embedding_model
    eval_vector_size = _configured_vector_size()

    if use_real_embeddings:
        embedding_mode = "real"
        log.info(
            "rag_eval.real_embeddings",
            embedding_model=configured_model,
            vector_size=eval_vector_size,
        )
        # The module-load patch is unconditional (so the offline /
        # CI path works without ceremony). Real mode needs to
        # restore the original ``embed_texts`` BEFORE any indexing
        # call, otherwise the mock leaks through and you get
        # mock-grade metrics with ``embedding_mode=real`` in the
        # log — a particularly confusing failure mode.
        _restore_embed_originals()
    else:
        embedding_mode = "mock"
        if not _embedding_is_patched():
            _patch_embed_topic_coded(dim=eval_vector_size)

    # ---- 3. Ensure Qdrant collection -----------------------------------
    # Done before DB writes so a Qdrant outage fails fast — no point
    # in provisioning a tenant we can't index into.
    await _ensure_collection_ready(vector_size=eval_vector_size)

    # ---- 4. Provision tenant + KB + articles ---------------------------
    tenants_def = dataset.get("tenants", {})
    kbs_def = dataset.get("knowledge_bases", {})
    main_tenant_def = tenants_def["tenant_eval_main"]
    other_tenant_def = tenants_def["tenant_eval_other"]
    main_kb_def = kbs_def["kb_eval_main"]

    main_tenant = await _create_tenant(name=main_tenant_def["name"])
    other_tenant = await _create_tenant(name=other_tenant_def["name"])

    main_kb = await _create_kb(
        tenant_id=main_tenant.id,
        name=main_kb_def["name"],
        slug=main_kb_def["slug"],
        # Honor the dataset's documented model for mock-mode runs
        # so the legacy contract is unchanged, but in real-embedding
        # mode pin the live configured model so the eval exercises
        # whatever the deployment actually uses. The dataset's
        # embedding_model field is treated as documentation in
        # real-embedding mode.
        embedding_model=(
            configured_model if use_real_embeddings
            else main_kb_def["embedding_model"]
        ),
        chunk_size=main_kb_def["chunk_size"],
        chunk_overlap=main_kb_def["chunk_overlap"],
    )

    # Map logical id -> real Article. The dataset references articles
    # by their logical ids ("article_seed_0001") so the report can be
    # read independently of the test run that produced it.
    logical_to_real: dict[str, str] = {}
    for article_def in dataset["articles"]:
        article, _version = await _create_article_with_version(
            tenant_id=main_tenant.id,
            knowledge_base_id=main_kb.id,
            title=article_def["title"],
            raw_text=article_def["text"],
        )
        logical_to_real[article_def["id"]] = article.id

    # ---- 5. Index all articles -----------------------------------------
    for article_def in dataset["articles"]:
        article_id = logical_to_real[article_def["id"]]
        result = await index_article(article_id=article_id)
        if result.status != ArticleStatus.INDEXED:
            raise RuntimeError(
                f"index_article failed for {article_def['id']!r}: "
                f"status={result.status.value}, "
                f"chunks_indexed={result.chunks_indexed}"
            )

    # ---- 6. Resolve query tenant + KB ids -----------------------------
    # ``tenant_eval_main`` -> the tenant we just created.
    # ``tenant_eval_other`` -> the other tenant we just created.
    # KBs are looked up by ``(tenant_id, slug)`` because the dataset
    # uses logical KB ids that may be shared across tenants.
    tenant_id_map: dict[str, str] = {
        "tenant_eval_main": main_tenant.id,
        "tenant_eval_other": other_tenant.id,
    }
    # The "other" tenant has no KB at the same ULID; we deliberately
    # use the main KB's id so the retriever raises
    # KnowledgeBaseNotFoundError (the cross-tenant isolation branch).

    # ---- 7. Run each query ---------------------------------------------
    per_query: list[dict] = []
    for query_def in dataset["queries"]:
        q_tenant = tenant_id_map[query_def["tenant_id"]]
        q_kb_id = main_kb.id  # the eval only owns one KB; cross-tenant
        # queries still target kb_eval_main's id, which won't exist
        # for the other tenant.
        q_text = query_def["text"]
        q_category = query_def.get("category", "standard")
        expected_article_ids = {
            logical_to_real[aid] for aid in query_def["expected_relevant_article_ids"]
        }

        record: dict = {
            "query_id": query_def["id"],
            "topic": query_def.get("topic"),
            "category": q_category,
            "text": q_text,
            "expected_relevant_article_ids": sorted(expected_article_ids),
            "predicted_article_ids": [],
            "predicted_scores": [],
            "first_relevant_rank": None,
            "isolation_handled": False,
        }

        try:
            results = await retrieve_chunks(
                tenant_id=q_tenant,
                knowledge_base_id=q_kb_id,
                query=q_text,
                top_k=top_k,
            )
        except KnowledgeBaseNotFoundError:
            # Cross-tenant isolation worked. Record the result and
            # continue — the metric for this query is naturally 0
            # because there are no hits.
            record["isolation_handled"] = True
            per_query.append(record)
            continue

        # Walk the retrieved chunks (already highest-score-first) and
        # capture the predicted article ids + scores.
        for hit in results:
            record["predicted_article_ids"].append(hit.article_id)
            record["predicted_scores"].append(round(hit.score, 6))

        # Find the rank of the first predicted article that is in the
        # expected set. Rank is 1-based; ``None`` means no hit.
        for rank, predicted_aid in enumerate(record["predicted_article_ids"], start=1):
            if predicted_aid in expected_article_ids:
                record["first_relevant_rank"] = rank
                break

        per_query.append(record)

    # ---- 8. Compute metrics --------------------------------------------
    hit_rate_at_1, hit_rate_at_5, mrr, ndcg_at_5 = _compute_overall_metrics(per_query)
    per_topic = _compute_per_topic_metrics(per_query)

    # Optional threshold sweep — Stage 10.3 calibration. Walk a grid of
    # cosine-similarity cutoffs and compute precision / recall across
    # the entire query set. The recommended threshold is the lowest
    # cutoff that hits ``recall >= 0.9`` while keeping
    # ``precision >= 0.5``; falling back to the threshold with the
    # best F1 if none meets both targets.
    threshold_sweep: list[dict] = []
    recommended_threshold: float | None = None
    if do_threshold_sweep:
        threshold_sweep, recommended_threshold = _compute_threshold_sweep(per_query)

    elapsed = time.perf_counter() - started

    report = EvalReport(
        dataset_version=dataset["version"],
        dataset_path=str(dataset_file.resolve()),
        embedding_mode=embedding_mode,
        total_queries=len(per_query),
        hit_rate_at_1=hit_rate_at_1,
        hit_rate_at_5=hit_rate_at_5,
        mrr=mrr,
        ndcg_at_5=ndcg_at_5,
        per_query=per_query,
        per_topic=per_topic,
        threshold_sweep=threshold_sweep,
        recommended_threshold=recommended_threshold,
        elapsed_seconds=round(elapsed, 3),
    )

    # ---- 9. Persist + summarize ----------------------------------------
    report_path = output / "eval_report.json"
    report_path.write_text(
        json.dumps(asdict(report), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    _print_summary(report)

    # ---- 10. Cleanup ---------------------------------------------------
    # Sweep Qdrant first so the cascade delete in Postgres doesn't
    # leave orphans (the retriever won't surface them, but a future
    # eval run on the same Qdrant collection shouldn't see them
    # either).
    await _cleanup_qdrant_for_tenant(tenant_id=main_tenant.id)
    await _cleanup_qdrant_for_tenant(tenant_id=other_tenant.id)
    await _delete_tenant(main_tenant.id)
    await _delete_tenant(other_tenant.id)

    log.info(
        "rag_eval.done",
        embedding_mode=report.embedding_mode,
        total_queries=report.total_queries,
        hit_rate_at_1=report.hit_rate_at_1,
        hit_rate_at_5=report.hit_rate_at_5,
        mrr=report.mrr,
        ndcg_at_5=report.ndcg_at_5,
        recommended_threshold=report.recommended_threshold,
        elapsed_seconds=report.elapsed_seconds,
    )
    return report


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------


def _compute_overall_metrics(per_query: list[dict]) -> tuple[float, float, float, float]:
    """Compute hit_rate@1, hit_rate@5, MRR, nDCG@5 across all queries.

    Edge cases (out_of_domain + cross_tenant) are included: their
    contribution is naturally 0 on every metric because their
    ``expected_relevant_article_ids`` is empty. Including them
    surfaces their behavior in the report (a future regression
    that returns irrelevant hits for an out-of-domain query
    would show up as nDCG > 0).
    """
    if not per_query:
        return 0.0, 0.0, 0.0, 0.0

    hits_at_1 = 0
    hits_at_5 = 0
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []
    for record in per_query:
        expected = set(record["expected_relevant_article_ids"])
        predicted = record["predicted_article_ids"]

        if not expected:
            # Out-of-domain or cross-tenant — the metric is 0
            # (no relevant in top-k, by construction). We still
            # compute nDCG so a regression that returns garbage
            # for an out-of-domain query would surface as nDCG>0.
            relevances = [
                1 if aid in expected else 0 for aid in predicted[:5]
            ]
            ndcgs.append(_ndcg_at_k(relevances, 5))
            reciprocal_ranks.append(0.0)
            continue

        # hit@1: top-1 in expected.
        if predicted and predicted[0] in expected:
            hits_at_1 += 1
        # hit@5: any of top-5 in expected.
        if any(aid in expected for aid in predicted[:5]):
            hits_at_5 += 1
        # MRR: 1/rank of first relevant hit.
        first_rank = record.get("first_relevant_rank")
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
        # nDCG: binary relevance.
        relevances = [1 if aid in expected else 0 for aid in predicted[:5]]
        ndcgs.append(_ndcg_at_k(relevances, 5))

    n = len(per_query)
    return (
        hits_at_1 / n,
        hits_at_5 / n,
        statistics.fmean(reciprocal_ranks),
        statistics.fmean(ndcgs),
    )


def _compute_per_topic_metrics(per_query: list[dict]) -> dict[str, dict]:
    """Aggregate metrics per topic. Edge cases are excluded.

    Per-topic aggregates include ONLY ``category == "standard"``
    records. The cross-tenant queries in the dataset carry a real
    ``topic`` field (the question is on-topic, just from the wrong
    tenant) but they would skew the topic rollups because every
    cross-tenant query has ``expected_relevant_article_ids == []`` —
    a real article for that topic exists, but the retriever can't
    see it. The per-topic metric should reflect retrieval quality
    for the queries the system COULD answer, so the cross-tenant
    cases stay in the global metric (where they correctly count as
    0 hits) but are filtered out of the per-topic rollup.

    ``expected_top_hit_position`` is recorded per-query for
    inspection but is not used in the aggregate — it is a
    *human-readable* expectation that "for this query, article X
    should rank #1". A future enhancement could turn it into a
    per-query pass/fail.
    """
    by_topic: dict[str, list[dict]] = defaultdict(list)
    for record in per_query:
        if record.get("category") != "standard":
            continue
        topic = record.get("topic")
        if topic is None:
            continue  # defensive — should not happen for category=standard
        by_topic[topic].append(record)

    out: dict[str, dict] = {}
    for topic, records in sorted(by_topic.items()):
        if not records:
            continue
        hr1, hr5, mrr, ndcg = _compute_overall_metrics(records)
        out[topic] = {
            "total_queries": len(records),
            "hit_rate_at_1": round(hr1, 4),
            "hit_rate_at_5": round(hr5, 4),
            "mrr": round(mrr, 4),
            "ndcg_at_5": round(ndcg, 4),
        }
    return out


def _compute_threshold_sweep(
    per_query: list[dict],
) -> tuple[list[dict], float | None]:
    """Walk a grid of cosine-similarity thresholds and report precision/recall.

    Stage 10.3 — calibrate ``RAGService.DEFAULT_SCORE_THRESHOLD``
    against the real embedding model. The eval records the per-query
    predicted scores (top-``k``); this function sweeps a grid of
    cutoffs and reports, for each:

    * ``n_kept`` — number of (query, hit) pairs that survive the
      cutoff. ``0`` means the cutoff dropped every hit.
    * ``precision`` — across kept pairs, fraction whose article id is
      in the expected set.
    * ``recall`` — fraction of (query, expected article) pairs that
      survived the cutoff.
    * ``f1`` — harmonic mean of precision and recall, with the usual
      ``f1=0`` when both are 0.

    The recommended threshold is the LOWEST cutoff that satisfies:

    * ``recall >= 0.9`` AND ``precision >= 0.5``

    If no cutoff meets both targets we fall back to the cutoff with
    the best F1, so the report always surfaces a single number the
    operator can paste into ``DEFAULT_SCORE_THRESHOLD``.

    Only ``category == "standard"`` queries participate in the sweep
    — out-of-domain and cross-tenant queries carry empty expected
    sets, so a hit "above the threshold" against them counts as a
    false positive that swamps the precision denominator. Filtering
    them out mirrors the per-topic rollup behavior.

    Returns:
        ``(rows, recommended)`` where ``rows`` is the sorted sweep
        table and ``recommended`` is the recommended cutoff (``None``
        when no query produced any hits at any threshold).
    """
    # Cosine similarity is bounded in [0, 1] for normalized vectors;
    # we sweep the interesting band 0.20 .. 0.70 in 0.05 steps.
    thresholds = [round(t * 0.05 + 0.20, 2) for t in range(0, 11)]
    rows: list[dict] = []

    # Pre-bucket standard records to avoid the category filter in the
    # inner loop. Empty expected sets are excluded.
    standard_records = [
        r
        for r in per_query
        if r.get("category") == "standard"
        and r.get("expected_relevant_article_ids")
        and r.get("predicted_scores")
    ]

    if not standard_records:
        # No real queries → nothing meaningful to sweep. Return the
        # empty grid + None recommendation so callers still get a
        # well-formed report.
        return rows, None

    # Total (query, expected article) pairs — the recall denominator.
    total_relevant = sum(len(r["expected_relevant_article_ids"]) for r in standard_records)

    best_f1 = -1.0
    best_f1_threshold: float | None = None
    recommended: float | None = None

    for t in thresholds:
        n_kept = 0
        n_true_positives = 0
        n_relevant_kept = 0
        for r in standard_records:
            expected = set(r["expected_relevant_article_ids"])
            for aid, score in zip(
                r["predicted_article_ids"], r["predicted_scores"]
            ):
                if score >= t:
                    n_kept += 1
                    if aid in expected:
                        n_true_positives += 1
                        n_relevant_kept += 1
        precision = (n_true_positives / n_kept) if n_kept else 0.0
        recall = (n_relevant_kept / total_relevant) if total_relevant else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) > 0
            else 0.0
        )
        rows.append(
            {
                "threshold": t,
                "n_kept": n_kept,
                "precision": round(precision, 4),
                "recall": round(recall, 4),
                "f1": round(f1, 4),
            }
        )

        # Track the F1-best cutoff as a fallback.
        if f1 > best_f1:
            best_f1 = f1
            best_f1_threshold = t

        # Recommend the LOWEST cutoff that meets both targets — we
        # prefer admitting more hits (lower threshold) as long as
        # recall stays at the floor.
        if (
            recommended is None
            and recall >= 0.9
            and precision >= 0.5
        ):
            recommended = t

    if recommended is None:
        recommended = best_f1_threshold

    return rows, recommended


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_dataset(dataset: dict) -> None:
    """Static checks on the loaded dataset.

    The eval doesn't trust a hand-edited dataset to be consistent;
    a missing required key should fail fast at load time rather
    than deep inside the indexing loop. The checks are intentionally
    minimal — anything more elaborate (schema validation, magic
    phrase presence) belongs in a dedicated validator module.
    """
    for key in ("version", "articles", "queries", "tenants", "knowledge_bases"):
        if key not in dataset:
            raise ValueError(f"dataset missing required key: {key!r}")
    if len(dataset["articles"]) < 1:
        raise ValueError("dataset has no articles")
    if len(dataset["queries"]) < 50:
        raise ValueError(
            f"dataset has only {len(dataset['queries'])} queries; M1 requires >= 50"
        )

    # Article ids must be unique.
    article_ids = [a["id"] for a in dataset["articles"]]
    if len(set(article_ids)) != len(article_ids):
        raise ValueError("dataset has duplicate article ids")

    # Each query's expected_relevant_article_ids must reference real
    # articles (when non-empty).
    valid_aids = set(article_ids)
    for q in dataset["queries"]:
        for aid in q.get("expected_relevant_article_ids", []):
            if aid not in valid_aids:
                raise ValueError(
                    f"query {q.get('id')!r} references unknown article {aid!r}"
                )


def _embedding_is_patched() -> bool:
    """Return True when the caller already monkeypatched ``embed_texts``.

    Used to avoid double-patching when ``run_eval`` is called from
    a pytest fixture that already installed a stub. We detect a
    patch by checking for a sentinel attribute on the worker module —
    if the runner itself installed the mock earlier, that sentinel
    will be present.
    """
    from knowledge import worker as worker_module

    return getattr(worker_module, "_rag_eval_patched", False)


# ---------------------------------------------------------------------------
# Summary printer
# ---------------------------------------------------------------------------


def _print_summary(report: EvalReport) -> None:
    """Print a human-readable summary to stdout.

    Kept simple: no third-party formatting libs. The output is
    plain text so it greppable in CI logs.
    """
    line = "=" * 72
    print(line)
    print("RAG EVAL SUMMARY")
    print(line)
    print(f"dataset         : {report.dataset_path}")
    print(f"version         : {report.dataset_version}")
    print(f"embedding_mode  : {report.embedding_mode}")
    print(f"queries         : {report.total_queries}")
    print(f"hit_rate@1      : {report.hit_rate_at_1:.4f}")
    print(f"hit_rate@5      : {report.hit_rate_at_5:.4f}")
    print(f"MRR             : {report.mrr:.4f}")
    print(f"nDCG@5          : {report.ndcg_at_5:.4f}")
    print(f"elapsed_seconds : {report.elapsed_seconds}")
    if report.recommended_threshold is not None:
        print()
        print(f"recommended_threshold (>=0.9 recall, >=0.5 precision): "
              f"{report.recommended_threshold:.2f}")
    print()
    print("PER-TOPIC (standard queries only)")
    print("-" * 72)
    print(f"{'topic':<25} {'N':>3} {'HR@1':>8} {'HR@5':>8} {'MRR':>8} {'nDCG@5':>8}")
    for topic, m in sorted(report.per_topic.items()):
        print(
            f"{topic:<25} "
            f"{m['total_queries']:>3} "
            f"{m['hit_rate_at_1']:>8.4f} "
            f"{m['hit_rate_at_5']:>8.4f} "
            f"{m['mrr']:>8.4f} "
            f"{m['ndcg_at_5']:>8.4f}"
        )
    if report.threshold_sweep:
        print()
        print("THRESHOLD SWEEP (cosine similarity, standard queries only)")
        print("-" * 72)
        print(f"{'thr':>5} {'n_kept':>7} {'precision':>10} {'recall':>8} {'f1':>8}")
        for row in report.threshold_sweep:
            marker = ""
            if report.recommended_threshold is not None and abs(
                row["threshold"] - report.recommended_threshold
            ) < 1e-6:
                marker = "  <- recommended"
            print(
                f"{row['threshold']:>5.2f} "
                f"{row['n_kept']:>7d} "
                f"{row['precision']:>10.4f} "
                f"{row['recall']:>8.4f} "
                f"{row['f1']:>8.4f}{marker}"
            )
    print(line)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI argument parser.

    The defaults point at the in-tree dataset + a ``./eval_output``
    directory relative to the current working directory. CI callers
    pass explicit paths.
    """
    parser = argparse.ArgumentParser(
        description="Run the offline RAG evaluation against a JSON dataset."
    )
    parser.add_argument(
        "--dataset",
        default=str(
            Path(__file__).resolve().parent / "rag_eval_set.json"
        ),
        help="Path to the eval dataset JSON (default: %(default)s).",
    )
    parser.add_argument(
        "--output-dir",
        default="./eval_output",
        help="Directory to write eval_report.json into (default: %(default)s).",
    )
    parser.add_argument(
        "--real-embeddings",
        action="store_true",
        help=(
            "Use the production ``embed_texts`` (routed to "
            "Doubao/OpenAI via ``DEFAULT_EMBEDDING_MODEL``) instead of "
            "the hash-seeded mock. Requires a valid embedding API key "
            "in the environment and a Qdrant collection at the "
            "matching vector dimension."
        ),
    )
    parser.add_argument(
        "--threshold-sweep",
        action="store_true",
        help=(
            "After the standard metric pass, walk a grid of cosine "
            "similarity cutoffs and report precision / recall + a "
            "recommended ``DEFAULT_SCORE_THRESHOLD`` for the live "
            "embedding model. Implies ``--top-k 10`` for a meaningful "
            "score distribution."
        ),
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        help=(
            "Number of hits ``retrieve_chunks`` should return per "
            "query (default: %(default)s). Use 10+ when sweeping "
            "thresholds so the score distribution covers more than "
            "the obvious top-5."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns 0 on success, 1 on error.

    Kept synchronous at the outer edge so it composes with
    ``python -m`` and shell pipelines. The async work happens
    inside ``run_eval`` via ``asyncio.run``.
    """
    args = _parse_args(argv)
    # Sanity: the threshold sweep is only meaningful with a wider
    # score distribution than the default top-5 — silently upgrade
    # top_k if the caller forgot to bump it.
    top_k = args.top_k
    if args.threshold_sweep and top_k < 10:
        top_k = 10
    try:
        asyncio.run(
            run_eval(
                dataset_path=args.dataset,
                output_dir=args.output_dir,
                use_real_embeddings=args.real_embeddings,
                do_threshold_sweep=args.threshold_sweep,
                top_k=top_k,
            )
        )
    except Exception as exc:
        print(f"rag_eval failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


# Mark the worker module so subsequent ``run_eval`` calls in the
# same process don't re-install the mock embedding stub. The
# ``_embedding_is_patched`` helper reads this sentinel.
def _mark_embedding_patched() -> None:
    from knowledge import worker as worker_module

    worker_module._rag_eval_patched = True  # type: ignore[attr-defined]


# NO module-level patch. Earlier versions of this file patched
# ``embed_texts`` at import time so an empty CLI invocation would
# "just work" for offline runs, but that made the real-embedding
# path fragile: any attempt to restore the originals cached the
# already-patched stub. The patch is now applied inside
# ``run_eval`` (mock mode only) so the worker + retriever see the
# real ``embed_texts`` until the eval explicitly asks for the mock.
#
# ``_embedding_is_patched`` still drives the "have we patched in
# this process" check used by re-entrant ``run_eval`` calls.


if __name__ == "__main__":
    sys.exit(main())
