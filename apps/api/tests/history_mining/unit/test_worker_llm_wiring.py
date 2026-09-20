"""Unit tests for the history-mining worker's LLM wiring (M4.A Task 4).

M4.A replaced ``LLMClient.with_config(provider=, model=)`` (removed in
Task 3) with the gateway-based pattern::

    gateway = LLMGateway(providers=build_provider_registry(settings))
    pinned = gateway.with_config(provider=, model=)
    LLMClient(provider_resolver=pinned, tenant_id=...)

These tests verify the worker actually uses that pattern: the closure
inside :func:`_process_tenant` produces ``LLMClient`` instances whose
``provider_resolver`` is a :class:`PinnedResolver`, and the
``(provider, model)`` pair comes from
``settings.history_mining_*`` (falling back to ``settings.qa_judge_*``).

The tests are pure-Python unit tests — no DB, no real embeddings, no
real providers. They patch every I/O boundary at the module level.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from llm_client.resolvers import PinnedResolver


def _stub_gateway() -> MagicMock:
    """Build a stub ``LLMGateway`` whose ``with_config`` records its
    arguments and returns a real :class:`PinnedResolver`.

    Using a real ``PinnedResolver`` (not a ``MagicMock``) lets the
    production ``_process_tenant`` closure produce an ``LLMClient``
    whose ``provider_resolver`` passes ``isinstance(resolver,
    PinnedResolver)`` checks — same as in production.
    """
    gateway = MagicMock()
    captured: dict[str, str] = {}

    def _with_config(*, provider: str, model: str) -> PinnedResolver:
        captured["provider"] = provider
        captured["model"] = model
        return PinnedResolver(
            provider=MagicMock(name=f"provider-{provider}"), model=model
        )

    gateway.with_config.side_effect = _with_config
    gateway.aclose_all = AsyncMock()
    gateway._captured = captured  # type: ignore[attr-defined]
    return gateway


def _settings_stub(
    *, mining_provider: str, mining_model: str,
    qa_judge_provider: str = "qa-fallback-provider",
    qa_judge_model: str = "qa-fallback-model",
) -> MagicMock:
    """Build a Settings-like MagicMock exposing the fields the worker reads."""
    s = MagicMock()
    s.history_mining_provider = mining_provider
    s.history_mining_model = mining_model
    s.qa_judge_provider = qa_judge_provider
    s.qa_judge_model = qa_judge_model
    s.history_mining_lookback_days = 30
    s.history_mining_min_cluster_size = 2
    s.history_mining_max_cluster_size = 100
    return s


def _mock_clusterer_with_one_central_clusters() -> MagicMock:
    """Return a stub HDBSCAN clusterer that yields a single cluster
    covering the first 5 indices of the input vectors.

    The closure inside ``_process_tenant`` builds drafts from these
    indices; we don't care about the draft content (the generator
    falls back on any LLM failure).
    """
    from history_mining.clusterer import Cluster

    cluster = MagicMock(spec=Cluster)
    cluster.id = 1
    cluster.indices = [0, 1, 2, 3, 4]

    clusterer = MagicMock()
    clusterer.cluster.return_value = [cluster]
    return clusterer


@pytest.mark.asyncio
async def test_process_tenant_factory_uses_pinned_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_process_tenant`` builds an ``_llm_factory`` closure that
    returns an ``LLMClient`` whose ``provider_resolver`` is a
    :class:`PinnedResolver` (not the default prefix router)."""
    from history_mining.worker import _process_tenant

    # Capture the factory the worker builds, so we can call it ourselves.
    captured_factory: dict[str, Any] = {}

    def _capturing_generator(*, llm_client_factory, **_kw):
        captured_factory["factory"] = llm_client_factory

        gen = MagicMock()

        async def _generate(questions):
            # Trigger the factory closure — the resolved LLMClient is
            # what we want to inspect.
            captured_factory["llm_client"] = llm_client_factory()
            from history_mining.draft_generator import KBDraft
            return KBDraft(title="t", body="b", suggested_tags=[])

        gen.generate = _generate
        return gen

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.add = MagicMock()
    fake_session.commit = AsyncMock()

    monkeypatch.setattr(
        "history_mining.worker.KBDraftGenerator", _capturing_generator
    )
    monkeypatch.setattr(
        "history_mining.worker.HdbscanClusterer",
        lambda **kw: _mock_clusterer_with_one_central_clusters(),
    )
    monkeypatch.setattr(
        "history_mining.worker.get_sessionmaker", lambda: MagicMock(return_value=fake_session)
    )
    monkeypatch.setattr(
        "history_mining.worker.get_settings",
        lambda: _settings_stub(
            mining_provider="anthropic", mining_model="claude-mining-1"
        ),
    )

    # Embedding succeeds (any non-empty vectors). ``embed_texts`` is
    # imported lazily inside ``_process_tenant`` — patch the source
    # module, not ``history_mining.worker``.
    async def _embed(texts, tenant_id=None, **_kw):
        from llm_client.types import EmbeddingResult
        return EmbeddingResult(
            vectors=[[1.0, 0.0, 0.0] for _ in texts],
            model="m",
            prompt_tokens=0,
            total_tokens=0,
        )

    monkeypatch.setattr("llm_client.embeddings.embed_texts", _embed)

    gateway = _stub_gateway()

    messages = [(f"m{i}", f"q{i}") for i in range(5)]
    await _process_tenant(
        tenant_id="t1",
        messages=messages,
        min_cluster_size=2,
        max_cluster_size=20,
        gateway=gateway,
    )

    # 1. gateway.with_config was called with the configured
    # (history_mining_provider, history_mining_model) pair.
    assert gateway._captured["provider"] == "anthropic"
    assert gateway._captured["model"] == "claude-mining-1"

    # 2. The factory closure returns an LLMClient whose
    # provider_resolver is a PinnedResolver (M4.A contract).
    llm = captured_factory["llm_client"]
    assert isinstance(llm.provider_resolver, PinnedResolver)
    assert llm.provider_resolver.model == "claude-mining-1"

    # 3. tenant_id is the pipeline-level one (usage attribution
    # rolls up at the pipeline level; per-tenant routing is M4.C).
    assert llm.tenant_id == "history-mining"


@pytest.mark.asyncio
async def test_process_tenant_falls_back_to_qa_judge_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``history_mining_*`` is empty, the worker falls back to
    ``qa_judge_*`` settings (backward-compat semantics preserved by
    the migration).
    """
    from history_mining.worker import _process_tenant

    captured: dict[str, Any] = {}

    def _capturing_generator(*, llm_client_factory, **_kw):
        captured["factory"] = llm_client_factory

        gen = MagicMock()

        async def _generate(questions):
            captured["llm_client"] = llm_client_factory()
            from history_mining.draft_generator import KBDraft
            return KBDraft(title="t", body="b", suggested_tags=[])

        gen.generate = _generate
        return gen

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=None)
    fake_session.add = MagicMock()
    fake_session.commit = AsyncMock()

    monkeypatch.setattr(
        "history_mining.worker.KBDraftGenerator", _capturing_generator
    )
    monkeypatch.setattr(
        "history_mining.worker.HdbscanClusterer",
        lambda **kw: _mock_clusterer_with_one_central_clusters(),
    )
    monkeypatch.setattr(
        "history_mining.worker.get_sessionmaker", lambda: MagicMock(return_value=fake_session)
    )
    # history_mining_* empty → fall back to qa_judge_*.
    monkeypatch.setattr(
        "history_mining.worker.get_settings",
        lambda: _settings_stub(
            mining_provider="",
            mining_model="",
            qa_judge_provider="minimax",
            qa_judge_model="minimax-judge-1",
        ),
    )

    async def _embed(texts, tenant_id=None, **_kw):
        from llm_client.types import EmbeddingResult
        return EmbeddingResult(
            vectors=[[1.0, 0.0, 0.0] for _ in texts],
            model="m",
            prompt_tokens=0,
            total_tokens=0,
        )

    monkeypatch.setattr("llm_client.embeddings.embed_texts", _embed)

    gateway = _stub_gateway()

    messages = [(f"m{i}", f"q{i}") for i in range(5)]
    await _process_tenant(
        tenant_id="t1",
        messages=messages,
        min_cluster_size=2,
        max_cluster_size=20,
        gateway=gateway,
    )

    # Falls back to qa_judge_provider / qa_judge_model.
    assert gateway._captured["provider"] == "minimax"
    assert gateway._captured["model"] == "minimax-judge-1"

    llm = captured["llm_client"]
    assert isinstance(llm.provider_resolver, PinnedResolver)
    assert llm.provider_resolver.model == "minimax-judge-1"


@pytest.mark.asyncio
async def test_history_mining_worker_closes_gateway_in_finally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``history_mining_worker`` MUST call ``gateway.aclose_all()``
    in a ``finally`` block — a mid-run crash (e.g. one bad tenant)
    must not leak the provider HTTP pool.
    """
    from history_mining import worker as worker_module

    gateway = _stub_gateway()

    # Patch at the source module — the worker imports these lazily
    # inside the function, so monkeypatching ``history_mining.worker.*``
    # has no effect (the name lookup happens at call time, but the
    # import itself completes before the patch runs).
    monkeypatch.setattr(
        "llm_client.provider_registry.build_provider_registry",
        lambda s: {"anthropic": MagicMock(name="stub-provider")},
    )
    monkeypatch.setattr(
        "llm_client.gateway.LLMGateway",
        lambda *, providers, **kw: gateway,
    )

    # Empty data path → returns early but still runs the finally.
    monkeypatch.setattr(
        "history_mining.worker.get_settings",
        lambda: _settings_stub(
            mining_provider="anthropic", mining_model="claude-mining-1"
        ),
    )

    # Empty result from the SQL query → early return.
    empty_session = MagicMock()
    empty_session.__aenter__ = AsyncMock(return_value=empty_session)
    empty_session.__aexit__ = AsyncMock(return_value=None)

    async def _empty_execute(*_a, **_kw):
        r = MagicMock()
        r.fetchall = MagicMock(return_value=[])
        return r

    empty_session.execute = _empty_execute
    monkeypatch.setattr(
        "history_mining.worker.get_sessionmaker",
        lambda: MagicMock(return_value=empty_session),
    )

    result = await worker_module.history_mining_worker(ctx={})

    assert result == {"processed": 0, "drafts_created": 0}
    # The finally block ran the gateway close even on early-return.
    gateway.aclose_all.assert_awaited_once()


@pytest.mark.asyncio
async def test_history_mining_worker_closes_gateway_on_tenant_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-tenant exception (caught and logged) still triggers the
    ``finally`` gateway close — defensive against partial-run
    provider leaks.
    """
    from history_mining import worker as worker_module

    gateway = _stub_gateway()
    monkeypatch.setattr(
        "llm_client.provider_registry.build_provider_registry",
        lambda s: {"anthropic": MagicMock(name="stub-provider")},
    )
    monkeypatch.setattr(
        "llm_client.gateway.LLMGateway",
        lambda *, providers, **kw: gateway,
    )
    monkeypatch.setattr(
        "history_mining.worker.get_settings",
        lambda: _settings_stub(
            mining_provider="anthropic", mining_model="claude-mining-1"
        ),
    )

    # DB returns one tenant + one message so the loop runs once.
    db_session = MagicMock()
    db_session.__aenter__ = AsyncMock(return_value=db_session)
    db_session.__aexit__ = AsyncMock(return_value=None)

    async def _execute_with_rows(*_a, **_kw):
        r = MagicMock()
        r.fetchall = MagicMock(return_value=[("t1", "m1", "q1")])
        return r

    db_session.execute = _execute_with_rows
    monkeypatch.setattr(
        "history_mining.worker.get_sessionmaker",
        lambda: MagicMock(return_value=db_session),
    )

    # _process_tenant blows up — per-tenant isolation catches it but
    # the finally block still runs.
    async def _explode(*_a, **_kw):
        raise RuntimeError("simulated clusterer crash")

    monkeypatch.setattr(worker_module, "_process_tenant", _explode)

    result = await worker_module.history_mining_worker(ctx={})

    # Per-tenant failure was swallowed; result is the empty summary.
    assert result["processed"] == 1
    assert result["drafts_created"] == 0
    # Gateway still closed in finally.
    gateway.aclose_all.assert_awaited_once()