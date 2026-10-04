"""Exception hierarchy for LLM client errors.

All providers raise these — callers should be able to handle them uniformly.
"""
from typing import NamedTuple


class LLMError(Exception):
    """Base exception for all LLM client errors."""


class ProviderUnavailable(LLMError):  # noqa: N818
    """Provider is unreachable (network error, 5xx response, timeout).

    Caller may retry with backoff or fall back to a different provider.
    """


class RateLimited(LLMError):  # noqa: N818
    """Provider returned 429. Caller should back off and retry.

    Carries `retry_after_seconds` if the provider supplied it.
    """

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class InvalidRequest(LLMError):  # noqa: N818
    """Request was malformed (bad model name, missing field, etc.).

    Caller should NOT retry — fix the request instead.
    """


class OutputInvalid(LLMError):  # noqa: N818
    """Provider returned something we couldn't parse (e.g. tool calls in
    unexpected format, JSON syntax error in structured-output mode).
    """


class AttemptRecord(NamedTuple):
    """Single attempt result inside a fallback chain.

    Carries the provider name + model + exception class name (string) so
    ops can see which steps were tried and why they failed. PII-safe:
    no message content, no request bodies — just identifiers + error class.

    Attributes:
        provider_name: gateway-registered provider name (e.g. "minimax").
        model: model name actually used for this attempt (may differ from
            request.model when steps override).
        exc_type: ``type(exc).__name__`` of the exception that aborted
            this step (e.g. "ProviderUnavailable", "RateLimited").
    """

    provider_name: str
    model: str
    exc_type: str


class FallbackChainExhausted(ProviderUnavailable):
    """All steps in a :class:`FallbackResolver` chain failed.

    Subclasses :class:`ProviderUnavailable` so callers that already
    handle that exception (e.g. an outer retry loop) treat chain
    exhaustion as a retryable infrastructure failure.

    Attributes:
        attempts: ordered list of every attempted step with its
            exception class. Useful for ops triage (``llm_client.
            fallback_chain_exhausted`` log line) and unit tests
            that want to assert on the attempt history.
    """

    def __init__(self, *, attempts: list[AttemptRecord]) -> None:
        self.attempts = list(attempts)
        providers = ", ".join(f"{a.provider_name}:{a.model}" for a in attempts)
        super().__init__(
            f"Fallback chain exhausted after {len(attempts)} attempt(s): "
            f"[{providers}]"
        )
