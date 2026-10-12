"""Mixin for custom exceptions to attach PII-safe attributes to their span.

When a custom exception is raised inside a span, the
``__otel_attrs__`` dict is recorded as a span event so operators can
see WHY a span failed without leaking PII into the trace.

Usage::

    class TenantBudgetExceeded(BudgetError):
        def __init__(self, tenant_id, period, ...):
            super().__init__(f"...")
            self.__otel_attrs__ = {
                "tenant_id": tenant_id,
                "budget.period": period,
            }
"""
from __future__ import annotations

from typing import Protocol


class OtelAttrsException(Protocol):
    """Mixin protocol — exceptions opt in by defining ``__otel_attrs__``."""

    __otel_attrs__: dict[str, object]
