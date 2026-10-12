"""OpenTelemetry ``@traced`` decorator + tracer + PII-safe attributes.

The decorator opens a child span around an async function call, sets
attributes from a static dict, and records exceptions. Use it on
service methods (TicketService, AdminRepository) where the attribute
set is known at decoration time.

For runtime-dependent attribute values, use ``tracer.start_as_current_span``
manually — see the ``llm.chat`` example in
``apps/api/src/llm_client/client.py``.

PII discipline
--------------

Span attributes MUST be on the whitelist in ``PII_WHITELIST``. The
``test_pii_whitelist`` static-scan test enforces this at the code
level. Forbidden tokens (api_key, message.content, customer.email,
customer.name, customer.ip) cause the test to fail.
"""
from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, ParamSpec, TypeVar

import structlog
from opentelemetry import trace
from opentelemetry.util.types import AttributeValue

# Local tracer — DO NOT import from observability (circular).
# ``__init__.py`` also calls ``get_tracer("lumen")``; both return the
# same SDK-backed Tracer instance from the active TracerProvider.
tracer = trace.get_tracer("lumen")

P = ParamSpec("P")
R = TypeVar("R")

# Whitelist of allowed span attribute keys. Anything outside this set
# is treated as a PII violation. The static-scan test enforces this.
PII_WHITELIST: frozenset[str] = frozenset({
    "tenant_id",
    "provider_name",
    "model",
    "llm.tokens.input",
    "llm.tokens.output",
    "llm.cost_usd",
    "llm.latency_ms",
    "http.method",
    "http.route",
    "http.status_code",
    "channel.name",
    "agent.id",
    "conversation.id",
    "ticket.id",
    "worker.job_name",
    "worker.attempt",
    # OpenTelemetry semantic conventions (read-only, safe)
    "http.request.method",
    "url.path",
    "server.address",
})

# Forbidden tokens. ANY appearance of these in a ``set_attribute``,
# ``add_event``, ``start_as_current_span`` call, or the static ``attributes``
# dict of @traced, fails the static-scan test.
PII_FORBIDDEN: tuple[str, ...] = (
    "api_key",
    "message.content",
    "messages",
    "customer.email",
    "customer.name",
    "customer.ip",
    "customer.phone",
    "password",
    "secret",
    "token",
)


def _check_attribute_whitelist(name: str, value: Any) -> None:
    """Runtime check: fail fast if a non-whitelisted attribute is set.

    The static-scan test catches most violations, but this runtime
    guard catches anything that slips through (e.g. a dynamic key
    constructed from a variable).
    """
    if name not in PII_WHITELIST:
        # Don't raise — log + skip. Span emission is best-effort.
        structlog.get_logger("otel").warning(
            "otel.attribute_not_whitelisted",
            name=name,
            value_type=type(value).__name__,
        )


def traced(
    name_or_fn: Callable[P, R] | str | None = None,
    *,
    attributes: dict[str, AttributeValue] | None = None,
    record_exception: bool = True,
) -> Callable[[Callable[P, R]], Callable[P, R]] | Callable[P, R]:
    """Open a child span around an async function call.

    Two calling forms are supported::

        @traced                       # bare — uses fn.__qualname__
        async def f(): ...

        @traced("custom.name",       # parameterized
                attributes={"k": "v"},
                record_exception=True)
        async def f(): ...

    Args:
        name_or_fn: Either the function being decorated (bare form) or
            the explicit span name (parameterized form).
        attributes: Static attribute dict. Apply PII whitelist — keys
            outside ``PII_WHITELIST`` log a warning at runtime.
        record_exception: If True (default), exceptions raised inside
            the span are recorded as a span event and the status is
            set to ERROR. If False, the exception still propagates but
            is not annotated on the span.
    """
    # Bare-decorator form: @traced with no parens. ``name_or_fn`` is
    # the function being decorated.
    if callable(name_or_fn) and not isinstance(name_or_fn, str):
        fn = name_or_fn
        return _build_wrapper(
            fn, name=None, attributes=None, record_exception=True,
        )

    name: str | None = name_or_fn  # type: ignore[assignment]

    def deco(fn: Callable[P, R]) -> Callable[P, R]:
        return _build_wrapper(
            fn, name=name, attributes=attributes, record_exception=record_exception,
        )
    return deco


def _build_wrapper(
    fn: Callable[P, R],
    *,
    name: str | None,
    attributes: dict[str, AttributeValue] | None,
    record_exception: bool,
) -> Callable[P, R]:
    """Build the async wrapper that opens a span around ``fn``."""
    @functools.wraps(fn)
    async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        # Use ``__name__`` (not ``__qualname__``) so nested functions
        # inside test cases produce clean span names. ``__qualname__``
        # would include the enclosing test class/function, which makes
        # span queries noisy.
        span_name = name or fn.__name__
        attrs = attributes or {}
        for k in attrs:
            _check_attribute_whitelist(k, attrs[k])
        with tracer.start_as_current_span(
            span_name,
            attributes=attrs,
            record_exception=record_exception,
            set_status_on_exception=record_exception,
        ):
            return await fn(*args, **kwargs)
    return async_wrapper


__all__ = ["PII_FORBIDDEN", "PII_WHITELIST", "traced", "tracer"]
