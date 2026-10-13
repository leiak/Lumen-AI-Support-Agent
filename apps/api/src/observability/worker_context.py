"""OpenTelemetry trace context propagation for Arq workers.

Arq doesn't auto-propagate context. These helpers serialize the
current OTel context into ``job_ctx`` on enqueue, and restore it on
the worker side. The result: a worker job is a child span of the
HTTP request that enqueued it.

Usage on the producer side (``channel.inbound._enqueue_qa_judge``)::

    await enqueue_with_trace(
        arq_redis, "qa_judge_task", job_ctx={}, message_id=ai_message.id,
    )

Usage on the consumer side (``qa.worker.qa_judge_task``)::

    async def qa_judge_task(ctx, message_id):
        with job_span("qa_judge", ctx, attempt=ctx.get("job_try", 1)):
            # spans emitted inside this block share the trace_id of
            # the HTTP request that enqueued this job.
            ...

Arq merges the ``job_ctx`` kwarg passed to ``enqueue_job`` into the
worker context ``ctx`` dict (see arq/worker.py), so reading
``ctx["traceparent"]`` on the consumer side gives back the carrier
key we injected at enqueue time.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

from opentelemetry import context as otel_context
from opentelemetry import propagate
from opentelemetry.context import Context

from observability import tracer


async def enqueue_with_trace(
    arq_pool: Any,  # arq.ArqRedis — not statically importable in tests
    job_name: str,
    *,
    job_ctx: dict[str, Any] | None = None,
    **job_kwargs: Any,
) -> Any:
    """Enqueue an Arq job with the current trace context in job_ctx.

    The job's ``job_ctx`` will carry a ``traceparent`` key. The worker
    uses :func:`extract_trace_from_job` (or :func:`job_span`) to
    restore the context and emit a child span.

    ``enqueue_job`` is an ``async`` method on ``arq.ArqRedis`` (arq
    >= 0.25), so this helper is async too. ``job_ctx=None`` is treated
    as ``{}``; existing keys are preserved (the ``traceparent`` key
    is added via a dict merge).
    """
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    final_ctx = {**(job_ctx or {}), **carrier}
    return await arq_pool.enqueue_job(job_name, job_ctx=final_ctx, **job_kwargs)


def extract_trace_from_job(job_ctx: dict[str, Any] | None) -> Context:
    """Restore the OTel context that was active when the job was enqueued.

    Returns the extracted context; pass it to ``attach()`` to make it
    current for subsequent span emissions. ``job_ctx=None`` and
    ``job_ctx={}`` both return the current (ambient) context — useful
    when the producer side never propagated a traceparent (e.g. an
    ad-hoc CLI enqueue) and the worker should still emit a root span
    rather than fail.
    """
    if not job_ctx:
        return otel_context.get_current()
    return propagate.extract(job_ctx)


@contextlib.contextmanager
def job_span(
    job_name: str,
    job_ctx: dict[str, Any] | None,
    *,
    attempt: int = 1,
) -> Iterator[Any]:
    """Context manager: open a ``worker.<job_name>`` span as a child of the
    enqueuing request's trace.

    Usage in an Arq worker::

        async def qa_judge_task(ctx, message_id):
            with job_span("qa_judge", ctx, attempt=ctx.get("job_try", 1)):
                ...

    ``job_ctx`` is the merged Arq context dict (which includes both
    the worker-level ctx and the ``job_ctx`` we injected at enqueue).
    ``attempt`` defaults to ``1`` for first-try jobs; pass
    ``ctx.get("job_try", 1)`` to surface retries as distinct spans.

    The extracted context is attached before the span opens and
    detached in a ``finally`` so a worker crash can't leak the
    attached context to the next job.
    """
    parent_ctx = extract_trace_from_job(job_ctx)
    token = otel_context.attach(parent_ctx)
    try:
        with tracer.start_as_current_span(
            f"worker.{job_name}",
            attributes={
                "worker.job_name": job_name,
                "worker.attempt": attempt,
            },
        ) as span:
            yield span
    finally:
        otel_context.detach(token)


__all__ = [
    "enqueue_with_trace",
    "extract_trace_from_job",
    "job_span",
]