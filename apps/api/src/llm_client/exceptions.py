"""Exception hierarchy for LLM client errors.

All providers raise these — callers should be able to handle them uniformly.
"""


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
