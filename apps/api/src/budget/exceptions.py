"""Pack B #8 — budget-side rate-limit exception.

Raised when BudgetResolver observes a 429 from the inner provider
chain AND remaining budget is below a configurable threshold. Signals
outer code: tenant has hit a budget wall while providers are still
rate-limiting — do not retry, surface as HTTP 429.
"""
from __future__ import annotations


class TenantBudgetRateLimited(Exception):  # noqa: N818
    """Raised by BudgetResolver post-mortem gate after observing 429.

    Attributes:
        tenant_id: The tenant whose budget is being enforced.
        period: Billing period the snapshot belongs to (``YYYY-MM``).
        remaining: ``effective_cap - tokens_used`` at gate fire time.
        threshold: The configured ``tenant_budget_429_skip_threshold_tokens``
            setting; ``remaining < threshold`` is what triggered the gate.
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        period: str,
        remaining: int,
        threshold: int,
    ) -> None:
        self.tenant_id = tenant_id
        self.period = period
        self.remaining = remaining
        self.threshold = threshold
        super().__init__(
            f"tenant {tenant_id} budget low in {period}: "
            f"remaining={remaining} < threshold={threshold} after 429"
        )


__all__ = ["TenantBudgetRateLimited"]
