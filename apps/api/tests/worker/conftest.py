"""Re-export the observability fixtures so ``tests/worker/`` tests
inherit the same ``memory_exporter`` setup.

The canonical conftest lives at
``tests/observability/conftest.py``; pytest's conftest loading is
parent-directory-scoped, so the observability fixtures aren't visible
from ``tests/worker/``. We import the module here so the underlying
``memory_exporter`` fixture (and the autouse
``clear_memory_exporter``) is registered for our subdir.

The SDK provider / in-memory exporter are installed at the
observability conftest's module-load time — BEFORE this conftest (or
``observability`` itself) is imported by any test. The tracer in
``observability/tracing.py`` is thus bound to the in-memory exporter
from the start.

M6.5 — without this re-export, ``tests/worker/`` sees the
``ProxyTracerProvider`` (no real SDK), so ``propagate.inject()`` skips
the invalid parent span and ``arq ``jobs are enqueued with an empty
``job_ctx``. M6.5's enqueue/extract tests fail with
``trace_id == '0000...'``.
"""
from __future__ import annotations

# Side-effect import: registers ``memory_exporter`` fixture (and the
# autouse ``clear_memory_exporter`` fixture) on this conftest's module,
# AND runs the module-level SDK setup in ``tests/observability/conftest``.
from tests.observability.conftest import (  # noqa: F401
    clear_memory_exporter,
    memory_exporter,
)
