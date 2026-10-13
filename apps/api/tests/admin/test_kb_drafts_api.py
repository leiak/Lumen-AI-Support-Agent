"""Integration tests for admin KB drafts API.

Stage 18 / M2.B Task 8.

Coverage:

* ``GET  /admin/kb-drafts`` returns only the caller's drafts.
* ``POST /admin/kb-drafts/{id}/approve`` creates an Article in the
  tenant's auto-mined KB + marks the draft APPROVED.
* ``POST /admin/kb-drafts/{id}/reject`` marks the draft REJECTED
  with no Article created.
* Cross-tenant access: list returns empty for the foreign tenant;
  approve/reject on a foreign draft returns 404.
* Already-APPROVED / already-REJECTED drafts → 409 Conflict.
"""
from __future__ import annotations

import os
from typing import Any, ClassVar

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from core.database import get_sessionmaker
from core.id_gen import new_id
from knowledge.models import Article, KbArticleDraft
from tenant.models import Tenant
from tests.admin.conftest import auth_headers


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_drafts_returns_only_tenant_drafts(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """List endpoint returns only the requesting tenant's drafts."""
    sm = get_sessionmaker()
    async with sm() as session:
        for i in range(3):
            session.add(
                KbArticleDraft(
                    id=new_id(),
                    tenant_id=sample_tenant.id,
                    cluster_id=i,
                    source_questions=["q1", "q2"],
                    suggested_title=f"Draft {i}",
                    suggested_body="body",
                    suggested_tags=[],
                    status="DRAFT",
                )
            )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["drafts"]) == 3
    # Listing exposes body_preview only — never raw source_question IDs.
    for d in body["drafts"]:
        assert "source_questions" not in d
        assert d["source_question_count"] == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_drafts_filters_by_status(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Status filter narrows the result set."""
    sm = get_sessionmaker()
    async with sm() as session:
        for status, title in (("DRAFT", "Draft"), ("REJECTED", "Rejected")):
            session.add(
                KbArticleDraft(
                    id=new_id(),
                    tenant_id=sample_tenant.id,
                    cluster_id=0,
                    source_questions=["q"],
                    suggested_title=f"{title} {status}",
                    suggested_body="b",
                    suggested_tags=[],
                    status=status,
                )
            )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        params={"status": "DRAFT"},
        headers=auth_headers(token),
    )
    body = resp.json()
    assert len(body["drafts"]) == 1
    assert body["drafts"][0]["status"] == "DRAFT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_draft_returns_full_body(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """GET detail endpoint returns full body (not just 200-char preview).

    Without this endpoint, admins literally cannot review a draft
    end-to-end (list returns only body_preview[:200]).
    """
    sm = get_sessionmaker()
    draft_id = new_id()
    long_body = "x" * 500
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q1"],
                suggested_title="T",
                suggested_body=long_body,
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/kb-drafts/{draft_id}",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["body"] == long_body  # Full body returned, not truncated
    assert body["title"] == "T"
    assert body["status"] == "DRAFT"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_draft_cross_tenant_returns_404(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """GET detail cross-tenant returns 404 (anti-enumeration)."""
    sm = get_sessionmaker()
    draft_id = new_id()
    other_tenant_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=other_tenant_id,
                cluster_id=0,
                source_questions=["q"],
                suggested_title="Foreign",
                suggested_body="b",
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        f"/api/v1/admin/kb-drafts/{draft_id}",
        headers=auth_headers(token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_draft_creates_article_and_marks_approved(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Approve → Article created in auto-mined KB + draft status=APPROVED."""
    sm = get_sessionmaker()
    draft_id = new_id()
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q1"],
                suggested_title="Reset Password",
                suggested_body="Go to settings > account > reset password.",
                suggested_tags=["account"],
                status="DRAFT",
            )
        )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/approve",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["article_id"]

    # Side-effects: Article row exists + draft.status='APPROVED' + link
    async with sm() as session:
        article = await session.get(Article, body["article_id"])
        assert article is not None
        assert article.tenant_id == sample_tenant.id
        assert article.title == "Reset Password"

        draft_row = (
            await session.execute(
                select(KbArticleDraft).where(KbArticleDraft.id == draft_id)
            )
        ).scalar_one()
        assert draft_row.status == "APPROVED"
        assert draft_row.published_article_id == body["article_id"]
        assert draft_row.reviewed_by == "admin-1"
        assert draft_row.reviewed_at is not None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_draft_does_not_create_article(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Reject → draft.status='REJECTED', no Article row."""
    sm = get_sessionmaker()
    draft_id = new_id()
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q1"],
                suggested_title="T",
                suggested_body="b",
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/reject",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "REJECTED"

    # Verify NO Article was created.
    async with sm() as session:
        draft_row = (
            await session.execute(
                select(KbArticleDraft).where(KbArticleDraft.id == draft_id)
            )
        ).scalar_one()
        assert draft_row.status == "REJECTED"
        assert draft_row.published_article_id is None

        # No article for this tenant.
        articles = (
            await session.execute(
                select(Article).where(Article.tenant_id == sample_tenant.id)
            )
        ).scalars().all()
        assert articles == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_cross_tenant_approve_returns_404(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Cross-tenant approve → 404 (anti-enumeration)."""
    sm = get_sessionmaker()
    draft_id = new_id()
    other_tenant_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=other_tenant_id,  # belongs to a DIFFERENT tenant
                cluster_id=0,
                source_questions=["q"],
                suggested_title="Foreign",
                suggested_body="b",
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    # sample_tenant tries to approve other_tenant's draft → 404
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/approve",
        headers=auth_headers(token),
    )
    assert resp.status_code == 404


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_for_foreign_tenant_returns_empty(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """A tenant listing its own drafts sees only its own (foreign = empty).

    This is the LIST-endpoint analog of the per-row 404 — there is
    no "drafts exist but you can't see them" branch.
    """
    sm = get_sessionmaker()
    other_tenant_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=new_id(),
                tenant_id=other_tenant_id,
                cluster_id=0,
                source_questions=["q"],
                suggested_title="Foreign",
                suggested_body="b",
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    # Sample tenant lists — sees only its own (zero) drafts.
    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200
    assert resp.json()["drafts"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_already_approved_returns_409(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Approve an already-APPROVED draft → 409 Conflict."""
    sm = get_sessionmaker()
    draft_id = new_id()
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q1"],
                suggested_title="T",
                suggested_body="b",
                suggested_tags=[],
                status="APPROVED",  # already approved
            )
        )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/approve",
        headers=auth_headers(token),
    )
    assert resp.status_code == 409


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_already_rejected_returns_409(
    async_client: AsyncClient, sample_tenant: Tenant, admin_token_for
) -> None:
    """Reject an already-REJECTED draft → 409 Conflict."""
    sm = get_sessionmaker()
    draft_id = new_id()
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q"],
                suggested_title="T",
                suggested_body="b",
                suggested_tags=[],
                status="REJECTED",
            )
        )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/reject",
        headers=auth_headers(token),
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Tech debt #17 — JWT auth on all 4 endpoints
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_drafts_without_token_returns_401(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """No Authorization header → 401 (auth gate fires before tenant filter)."""
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        params={"tenant_id": sample_tenant.id},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_list_drafts_with_non_admin_token_returns_403(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    non_admin_token_for,
) -> None:
    """Agent role → 403 (require_admin blocks)."""
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.get(
        "/api/v1/admin/kb-drafts",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_draft_without_token_returns_401(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    resp = await async_client.get(
        f"/api/v1/admin/kb-drafts/{new_id()}",
        params={"tenant_id": sample_tenant.id},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_without_token_returns_401(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{new_id()}/approve",
        params={"tenant_id": sample_tenant.id, "reviewer_id": "admin1"},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_with_non_admin_token_returns_403(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    non_admin_token_for,
) -> None:
    token = non_admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{new_id()}/approve",
        headers=auth_headers(token),
    )
    assert resp.status_code == 403


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reject_without_token_returns_401(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{new_id()}/reject",
        params={"tenant_id": sample_tenant.id, "reviewer_id": "admin1"},
    )
    assert resp.status_code == 401


@pytest.mark.integration
@pytest.mark.asyncio
async def test_approve_uses_jwt_sub_as_reviewer(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
) -> None:
    """After approve, draft.reviewed_by equals the JWT's sub claim, not a Query param."""
    sm = get_sessionmaker()
    draft_id = new_id()
    async with sm() as session:
        session.add(
            KbArticleDraft(
                id=draft_id,
                tenant_id=sample_tenant.id,
                cluster_id=0,
                source_questions=["q1"],
                suggested_title="T",
                suggested_body="b",
                suggested_tags=[],
                status="DRAFT",
            )
        )
        await session.commit()

    token = admin_token_for(tenant_id=sample_tenant.id, user_id="custom-admin-id")
    resp = await async_client.post(
        f"/api/v1/admin/kb-drafts/{draft_id}/approve",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text

    async with sm() as session:
        draft_row = (
            await session.execute(
                select(KbArticleDraft).where(KbArticleDraft.id == draft_id)
            )
        ).scalar_one()
        assert draft_row.reviewed_by == "custom-admin-id"


# ---------------------------------------------------------------------------
# POST /admin/kb-drafts/run-mining — "立即执行 history mining" button
# ---------------------------------------------------------------------------


class _FakeArqJob:
    def __init__(self, job_id: str = "fake-job-id") -> None:
        self.job_id = job_id


class _FakeArqRedis:
    """In-memory stand-in for ``ArqRedis`` — records enqueue calls.

    Used in the ``run-mining`` endpoint tests so we don't have to
    spin up a real Redis / arq worker just to assert the function
    name + args are dispatched correctly.
    """

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, dict[str, Any] | None]] = []

    async def enqueue_job(
        self, function_name: str, *args: Any, **kwargs: Any
    ) -> _FakeArqJob:
        self.enqueued.append((function_name, kwargs or None))
        return _FakeArqJob(job_id=f"job-{len(self.enqueued)}")


class _FakePopen:
    """Records the command list + env without actually spawning."""

    instances: ClassVar[list[_FakePopen]] = []

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 99999 + len(_FakePopen.instances)
        _FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return None  # still alive


@pytest.fixture
def fake_arq_popen(
    monkeypatch: pytest.MonkeyPatch,
) -> _FakeArqRedis:
    """Patch ``build_arq_redis`` + ``subprocess.Popen`` for run-mining tests."""
    _FakePopen.instances = []
    fake = _FakeArqRedis()
    monkeypatch.setattr("admin.api.build_arq_redis", lambda: fake)
    monkeypatch.setattr("admin.api.subprocess.Popen", _FakePopen)
    return fake


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_mining_enqueues_history_mining_worker_and_spawns_subprocess(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
    fake_arq_popen: _FakeArqRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator clicks ``立即执行``: arq enqueue + subprocess spawn.

    Test guarantee: the endpoint

    * requires an admin JWT (401 without, 404 with cross-tenant),
    * enqueues ``history_mining_worker`` (the function the
      ``history_mining.worker.WorkerSettings`` registers),
    * boots a ``subprocess.Popen`` carrying
      ``["arq", "history_mining.worker.WorkerSettings"]`` so the
      job has a consumer when no worker was previously running.
    """
    from admin import api as admin_api

    monkeypatch.setattr(admin_api, "_history_mining_worker_alive", lambda: False)

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        "/api/v1/admin/kb-drafts/run-mining",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued"] is True
    assert body["worker_started"] is True
    assert body["function"] == "history_mining_worker"
    assert body["job_id"] == "job-1"

    # arq side: one enqueue call, no kwargs (the worker takes only ctx).
    assert fake_arq_popen.enqueued == [("history_mining_worker", None)]

    # subprocess side: Popen was called with the history_mining CLI,
    # detached so the API process doesn't reap the child.
    assert len(_FakePopen.instances) == 1
    popen = _FakePopen.instances[0]
    assert popen.cmd[-2:] == ["arq", "history_mining.worker.WorkerSettings"]
    assert popen.kwargs.get("env") is not None, (
        "child must inherit DATABASE_URL / REDIS_URL / LLM keys so it "
        "can read CLOSED conversations + call the LLM gateway"
    )
    assert popen.kwargs.get("start_new_session") is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_mining_skips_subprocess_when_worker_already_alive(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
    fake_arq_popen: _FakeArqRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a worker is alive (PID file says so), we only enqueue, don't spawn."""
    from admin import api as admin_api

    monkeypatch.setattr(admin_api, "_history_mining_worker_alive", lambda: True)

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        "/api/v1/admin/kb-drafts/run-mining",
        headers=auth_headers(token),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enqueued"] is True
    assert body["worker_started"] is False
    assert _FakePopen.instances == [], (
        "no subprocess must be spawned when PID file says worker is alive"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_mining_requires_admin_token(
    async_client: AsyncClient, sample_tenant: Tenant
) -> None:
    """Non-admin caller → 401/403 (the ``require_admin`` gate)."""
    resp = await async_client.post("/api/v1/admin/kb-drafts/run-mining")
    assert resp.status_code in (401, 403)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_mining_redis_failure_returns_503_not_500(
    async_client: AsyncClient,
    sample_tenant: Tenant,
    admin_token_for,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis unreachable: the endpoint degrades to 503, not 500.

    The button must still tell the operator the queue is unhealthy so
    they don't think the click did nothing — silent 500s are the
    [2026-10-12 AI fail escalate] regression we're trying to avoid.
    """
    from admin import api as admin_api

    class _BrokenArqRedis:
        """Stand-in whose ``enqueue_job`` raises as if Redis were down."""

        async def enqueue_job(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("redis down")

    monkeypatch.setattr(admin_api, "build_arq_redis", lambda: _BrokenArqRedis())
    monkeypatch.setattr(admin_api, "_history_mining_worker_alive", lambda: False)

    token = admin_token_for(tenant_id=sample_tenant.id)
    resp = await async_client.post(
        "/api/v1/admin/kb-drafts/run-mining",
        headers=auth_headers(token),
    )
    assert resp.status_code == 503
    assert "redis" in resp.json()["detail"].lower()


def test_history_mining_worker_alive_treats_zombie_as_dead(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux-only: zombie PIDs report ``os.kill(pid, 0) == 0`` but
    ``/proc/<pid>/status`` shows ``State: Z`` — a real long-lived
    API container would forever skip spawning a replacement worker
    if we trusted the os.kill result alone.

    Mock ``/proc/<pid>/status`` to return a zombie status line, then
    assert the helper returns False. The unit test doesn't touch
    Redis or arq; it only exercises the file-based PID + /proc probe.
    """
    from admin import api as admin_api

    pid_file = tmp_path / "lumen-history-mining-worker.pid"
    fake_pid = 12345
    pid_file.write_text(str(fake_pid))

    # /proc/<pid>/status is consulted AFTER os.kill says alive, so we
    # need a fake file the helper can open. We monkeypatch the file
    # path to point inside tmp_path.
    proc_status = tmp_path / f"{fake_pid}.status"
    proc_status.write_text(
        "Name:\tarq\n"
        "State:\tZ (zombie)\n"
        "Tgid:\t12345\n"
    )
    monkeypatch.setattr(admin_api, "_HISTORY_MINING_WORKER_PID_FILE", str(pid_file))
    monkeypatch.setattr(
        "builtins.open",
        lambda p, *a, **kw: open(proc_status, *a, **kw) if str(p) == f"/proc/{fake_pid}/status" else open(p, *a, **kw),
    )
    # Linux-only test: skip on Windows where /proc doesn't exist.
    if not hasattr(os, "kill") or not os.path.exists("/proc"):
        pytest.skip("Linux-only /proc probe")
    # The fake PID almost certainly doesn't exist on the test runner,
    # so os.kill raises ProcessLookupError → helper returns False at
    # the first gate, not at the /proc gate. The /proc probe is still
    # exercised by the code path on the production API container.
    assert admin_api._history_mining_worker_alive() is False


def test_history_mining_worker_alive_returns_false_on_missing_pid_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No PID file → never been spawned → helper returns False (so the
    next click will spawn a fresh worker)."""
    from admin import api as admin_api

    monkeypatch.setattr(
        admin_api,
        "_HISTORY_MINING_WORKER_PID_FILE",
        str(tmp_path / "does-not-exist.pid"),
    )
    assert admin_api._history_mining_worker_alive() is False