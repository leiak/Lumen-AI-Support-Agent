"""Widget-test fixtures.

Stage 9.10 added an ``Origin`` allowlist check to
``widget/ws/router.websocket_endpoint``. starlette's ``TestClient``
does NOT emit an ``Origin`` header on its WebSocket handshake (its
default scope sets ``host: testserver`` but no ``Origin``), so every
existing widget WS unit test would break with a ``WebSocketDisconnect``
from the new origin check — even though those tests are about token
authentication, frame loops, or message persistence, not origins.

This fixture swaps ``is_origin_allowed`` for a permissive test-only
function so the existing tests run unchanged. The dedicated
``test_origin_validation.py`` suite monkeypatches ``is_origin_allowed``
back to the real implementation via its own autouse fixture and
exercises the rejection / normalization paths in isolation.

Production behavior is unchanged: this conftest never runs outside
the test runner.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _relax_widget_origin_check(monkeypatch: pytest.MonkeyPatch) -> None:
    """Permit every Origin on the widget WS endpoint for existing tests."""
    from widget.ws import router as ws_router_module

    def _allow_any(_origin: str | None, *, allowlist: list[str]) -> bool:
        return True

    monkeypatch.setattr(ws_router_module, "is_origin_allowed", _allow_any)
    yield