"""Re-export the observability fixtures so ``tests/agent/`` tests
inherit the same ``memory_exporter`` setup.

The canonical conftest lives at
``tests/observability/conftest.py``; pytest's conftest loading is
parent-directory-scoped, so the observability fixtures aren't visible
from ``tests/agent/``. We import the module here so the underlying
``memory_exporter`` fixture (and the autouse
``clear_memory_exporter``) is registered for our subdir.

The SDK provider / in-memory exporter are installed at the
observability conftest's module-load time — BEFORE this conftest (or
``observability`` itself) is imported by any test. The tracer in
``observability/tracing.py`` is thus bound to the in-memory exporter
from the start.
"""
from __future__ import annotations

# Side-effect import: registers ``memory_exporter`` fixture (and the
# autouse ``clear_memory_exporter`` fixture) on this conftest's module.
from tests.observability.conftest import (  # noqa: F401
    clear_memory_exporter,
    memory_exporter,
)
