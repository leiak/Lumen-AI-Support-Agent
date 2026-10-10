"""Pin the ``get_ticket_service`` dependency shape (no DB needed).

Regression: ``get_ticket_service`` was previously declared as
``Depends(get_session)`` where ``get_session`` is an
``@asynccontextmanager`` — FastAPI's DI would receive the
context-manager object and fail to ``__aiter__`` it
(``_AsyncGeneratorContextManager object is not an async iterator``),
turning every ``/api/v1/tickets/*`` route into a 500. The fix is a
generator dep that opens a fresh ``AsyncSession`` for the request
via ``get_sessionmaker()()`` and yields the wired ``TicketService``.

These tests assert only the dep's *shape* (it's an async generator
function, it depends on get_current_user, it does not silently
take a session) — they do not exercise the actual DB or the
service, so they run without a Postgres connection.
"""
from __future__ import annotations

import inspect
import typing
from typing import get_type_hints

import pytest

from ticket.api import get_ticket_service


def _all_depends(dep_func) -> list:
    """Walk the function's signature and return every ``Depends``
    instance referenced via ``Annotated[..., Depends(...)]``.

    The plain ``p.default`` is ``inspect._empty`` for
    ``Annotated[X, Depends(Y)]`` parameters — the Depends lives in
    the annotation's metadata, not in the default slot. Use
    ``get_type_hints(..., include_extras=True)`` to keep the
    ``typing.Annotated[...]`` wrapper and walk its ``__metadata__``.

    Depends is a public FastAPI class; the public surface is
    ``.dependency`` (the wrapped callable). We duck-type on that
    attribute rather than ``isinstance`` to avoid surprises if
    FastAPI ever changes the marker to a dataclass.
    """
    hints = get_type_hints(dep_func, include_extras=True)
    sig = inspect.signature(dep_func)
    deps: list = []
    for name, param in sig.parameters.items():
        if name not in hints:
            continue
        ann = hints[name]
        # ``Annotated[X, dep_a, dep_b, ...]`` — typing.get_args
        # returns (X, dep_a, dep_b).
        if typing.get_origin(ann) is typing.Annotated:
            for meta in typing.get_args(ann)[1:]:
                if hasattr(meta, "dependency") and callable(
                    getattr(meta, "dependency", None)
                ):
                    deps.append(meta)
    return deps


def test_get_ticket_service_is_async_generator_function() -> None:
    """The dep MUST be an async generator — that's what makes
    ``async with sm() as session: yield ...`` work inside FastAPI's
    DI and gives us per-request session lifecycle (commit on clean
    exit, rollback on exception).
    """
    assert inspect.isasyncgenfunction(get_ticket_service), (
        "get_ticket_service must be `async def ... yield ...` so "
        "FastAPI runs the body, gives the value to the route, and "
        "runs the cleanup on the way out. A regular `def` would not "
        "open a per-request session; a `def` returning a value would "
        "not close it."
    )


def test_get_ticket_service_depends_on_current_user() -> None:
    """The dep must depend on ``get_current_user`` so auth runs
    before the session is opened. Without this, a request with a
    missing/expired token would still open a session, leak it via
    the async-generator-cleanup path, and return 401 after
    unnecessary DB work.
    """
    from auth.dependencies import get_current_user

    deps = _all_depends(get_ticket_service)
    assert any(d.dependency is get_current_user for d in deps), (
        "get_ticket_service must use `Annotated[..., Depends(get_current_user)]` "
        "so auth ordering is enforced by the framework, not by "
        "discipline at the call site. Found: "
        f"{[d.dependency.__name__ for d in deps]}"
    )


def test_get_ticket_service_does_not_inject_session_directly() -> None:
    """Pin the original bug: ``get_session`` is an
    ``@asynccontextmanager``, not a session factory. Wiring it as
    ``Depends(get_session)`` made FastAPI's DI hand the
    context-manager object to the route, which then tried to
    ``__aiter__`` it. The session must come from
    ``get_sessionmaker()()`` *inside* the generator body, never
    via Depends.
    """
    from core.database import get_session  # noqa: F401 — referenced only

    deps = _all_depends(get_ticket_service)
    offending = [d for d in deps if d.dependency is get_session]
    assert not offending, (
        "get_ticket_service must NOT use `Depends(get_session)` — "
        "get_session is an @asynccontextmanager, not a session "
        "factory. Open the session via "
        "`async with get_sessionmaker()() as session:` inside the "
        "generator body."
    )


def test_get_ticket_service_signature_is_pure() -> None:
    """Sanity: the dep has the expected minimal surface (one
    auth-only parameter, no extra junk that would make a future
    refactor accidentally drop the generator semantics).
    """
    sig = inspect.signature(get_ticket_service)
    # Single param: the auth dep. If a second param shows up (e.g.
    # someone re-adds a `session: AsyncSession = Depends(...)`),
    # this test will surface it for review.
    assert len(sig.parameters) == 1, (
        f"get_ticket_service should have exactly one parameter "
        f"(the auth dep). Got {len(sig.parameters)}: "
        f"{list(sig.parameters)}"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
