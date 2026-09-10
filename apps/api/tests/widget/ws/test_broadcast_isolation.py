"""Channel-scoped broadcast isolation tests for ``ConnectionManager``.

Unit-style tests (no DB) that exercise the real ``ConnectionManager``
against fake WebSocket doubles. They prove ``broadcast_to_channel`` is
strictly channel-scoped: only connections whose ``channel_id`` matches
receive the frame, even when other connections on the same tenant use
different channel ids.

Tenant isolation in M1 is enforced at the connection layer in
``widget/ws/router.py`` (the token's ``tenant_id`` must match the
channel's ``tenant_id`` before the connection is registered), so once
a connection exists it is always tenant-correct, and channel scoping
on top is sufficient to keep frames from leaking across tenants.

Why feed fakes straight into ``manager.connect`` instead of going through
``TestClient.websocket_connect``? The same cross-loop trade-off
documented in
``tests/conversation/integration/test_conversation_lifecycle.py::test_ws_broadcast_carries_ai_reply``
applies here: ``TestClient``'s anyio portal runs on a separate event
loop. Driving the manager directly keeps everything on the test's loop,
which is also exactly how ``channel.inbound._broadcast_ai_complete``
calls into the manager in production (no portal in sight).
"""
from __future__ import annotations

import pytest

from widget.ws.manager import ConnectionManager


class FakeWebSocket:
    """Minimal WebSocket double: records ``send_json`` payloads into a list."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)


@pytest.fixture
def manager() -> ConnectionManager:
    """A fresh ``ConnectionManager`` per test — no shared singleton state."""
    return ConnectionManager()


async def test_broadcast_to_channel_only_reaches_matching_channel(
    manager: ConnectionManager,
) -> None:
    """``broadcast_to_channel(ch_a)`` must reach ch_a connections and
    nothing else — regardless of tenant_id collisions.

    Setup:
        - tenant_A, channel_a -> ws_a
        - tenant_A, channel_b -> ws_b
        - tenant_B, channel_c -> ws_c
    Even though ws_a and ws_b share tenant_A, ``broadcast_to_channel`` is
    channel-scoped, so ws_b is the "wrong channel" and must not receive.
    """
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    ws_c = FakeWebSocket()

    await manager.connect(
        websocket=ws_a, channel_id="ch_a", tenant_id="tenant_A",
        external_user_id="u_a",
    )
    await manager.connect(
        websocket=ws_b, channel_id="ch_b", tenant_id="tenant_A",
        external_user_id="u_b",
    )
    await manager.connect(
        websocket=ws_c, channel_id="ch_c", tenant_id="tenant_B",
        external_user_id="u_c",
    )

    payload = {"type": "message.complete", "conversation_id": "conv_1"}
    delivered = await manager.broadcast_to_channel("ch_a", payload)

    assert delivered == 1
    assert ws_a.sent == [payload]
    assert ws_b.sent == []
    assert ws_c.sent == []
