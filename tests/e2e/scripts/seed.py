"""Idempotent E2E seed for Lumen AI Support Agent.

Creates a deterministic demo tenant + agent user + web channel + one
PENDING conversation with a single customer message. Re-running this
script is safe: every row is created with a fixed primary key, so a
second invocation hits the unique constraint on ``users(tenant_id,
email)`` and we swallow that into a no-op.

Run from inside ``apps/api`` so the existing ``uv`` venv + .env are
picked up::

    cd apps/api && uv run python ../../tests/e2e/scripts/seed.py

Invoked by ``tests/e2e/global-setup.ts`` before the Playwright suite
runs. Lives under ``tests/e2e/`` only — does NOT touch apps/api
source code.

Anti-enumeration: this script is for tests. The credentials it
creates (``agent@demo.test`` / ``Demo123!``) MUST NOT exist in
production databases; treat them as ephemeral test data.
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

# Anchor ``apps/api/src`` onto sys.path so we can import the project's
# domain modules (UserRepository, hash_password, ChannelService, ...)
# without installing the package. Mirrors apps/api/conftest.py.
#
# Walk up from this file until we find the repo root (the directory
# that owns both ``apps/`` and ``tests/``). This is robust against
# being invoked from any cwd — the only invariant is that the script
# lives under ``<repo>/tests/e2e/scripts/``.
def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "apps" / "api" / "src").is_dir() and (
            candidate / "tests" / "e2e"
        ).is_dir():
            return candidate
    raise RuntimeError(
        f"could not locate repo root from {start} — "
        "expected to find apps/api/src and tests/e2e as siblings",
    )


_API_SRC = _find_repo_root(Path(__file__)) / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))

from auth.password import hash_password  # noqa: E402
from channel.enums import ChannelStatus, ChannelType  # noqa: E402
from channel.models import Channel  # noqa: E402
from conversation.enums import ConversationStatus, MessageRole  # noqa: E402
from conversation.models import Conversation, Message  # noqa: E402
from core.database import get_session, reset_engine, reset_sessionmaker  # noqa: E402
from core.id_gen import new_id  # noqa: E402
from tenant.enums import TenantPlan, TenantStatus, UserRole  # noqa: E402
from tenant.models import Tenant, User  # noqa: E402


# Deterministic IDs (26 chars, Crockford Base32-compatible). The
# backend stores tenant_id / user_id / channel_id as ``String(26)``
# without strict ULID validation — fixed ids let the Playwright
# fixtures assert against them without round-tripping the DB.
DEMO_TENANT_ID = "01HZDEMO00000000000000000"
DEMO_AGENT_USER_ID = "01HZDEMO00000000000000001"
DEMO_ADMIN_USER_ID = "01HZDEMO00000000000000002"
DEMO_CHANNEL_ID = "01HZDEMO00000000000000003"
DEMO_CONVERSATION_ID = "01HZDEMO00000000000000004"

# Public demo creds — referenced from the E2E specs and the README.
DEMO_AGENT_EMAIL = "agent@demo.test"
DEMO_ADMIN_EMAIL = "admin@demo.test"
DEMO_PASSWORD = "Demo123!"  # meets backend min_length=8.

# A single seeded customer message gives the AI-suggest endpoint a
# "last customer text" to operate on, so Flow A's "拿 AI 建议" step
# returns a real suggestion (or the documented fallback) instead of
# "no_customer_message".
DEMO_CUSTOMER_TEXT = "How do I reset my password?"
CUSTOMER_EXTERNAL_ID = "e2e-visitor-001"


async def _seed_tenant(session) -> None:
    if await session.get(Tenant, DEMO_TENANT_ID) is not None:
        return
    session.add(
        Tenant(
            id=DEMO_TENANT_ID,
            name="Acme Demo",
            plan=TenantPlan.PRO,
            status=TenantStatus.ACTIVE,
            settings={"source": "e2e-seed"},
        )
    )
    await session.flush()


async def _seed_user(
    session,
    *,
    user_id: str,
    email: str,
    role: UserRole,
    full_name: str,
) -> None:
    # The codebase's UserRepository.create() generates a fresh ULID
    # for ``id`` — for deterministic fixture ids we go through the
    # session directly. The hash matches what /auth/login expects.
    if await session.get(User, user_id) is not None:
        return
    session.add(
        User(
            id=user_id,
            tenant_id=DEMO_TENANT_ID,
            email=email,
            password_hash=hash_password(DEMO_PASSWORD),
            role=role,
            full_name=full_name,
            is_active=True,
        )
    )
    await session.flush()


async def _seed_channel(session) -> None:
    if await session.get(Channel, DEMO_CHANNEL_ID) is not None:
        return
    session.add(
        Channel(
            id=DEMO_CHANNEL_ID,
            tenant_id=DEMO_TENANT_ID,
            type=ChannelType.WEB,
            name="demo-web",
            credentials_encrypted=json.dumps({"origin": "http://localhost:8080"}),
            status=ChannelStatus.ACTIVE,
            config_json={},
        )
    )
    await session.flush()


async def _seed_conversation(session) -> None:
    if await session.get(Conversation, DEMO_CONVERSATION_ID) is not None:
        return
    now = datetime.now(UTC)
    session.add(
        Conversation(
            id=DEMO_CONVERSATION_ID,
            tenant_id=DEMO_TENANT_ID,
            channel_id=DEMO_CHANNEL_ID,
            customer_external_id=CUSTOMER_EXTERNAL_ID,
            status=ConversationStatus.PENDING,
            # Sits in the unassigned queue — Flow A exercises the
            # POST /claim endpoint to take ownership.
            assigned_agent_id=None,
            ai_handling=False,
            opened_at=now,
            last_activity_at=now,
        )
    )
    await session.flush()
    session.add(
        Message(
            id=new_id(),
            conversation_id=DEMO_CONVERSATION_ID,
            role=MessageRole.CUSTOMER,
            content_text=DEMO_CUSTOMER_TEXT,
            sender_id=None,
            created_at=now,
        )
    )


async def main() -> None:
    """Run the seed inside a single async session and commit once."""
    try:
        async with get_session() as session:
            await _seed_tenant(session)
            await _seed_user(
                session,
                user_id=DEMO_AGENT_USER_ID,
                email=DEMO_AGENT_EMAIL,
                role=UserRole.AGENT,
                full_name="Demo Agent",
            )
            await _seed_user(
                session,
                user_id=DEMO_ADMIN_USER_ID,
                email=DEMO_ADMIN_EMAIL,
                role=UserRole.ADMIN,
                full_name="Demo Admin",
            )
            await _seed_channel(session)
            await _seed_conversation(session)
            await session.commit()
    finally:
        # The seed used its own event loop; the engine bound to that
        # loop is now unusable. Mirrors apps/api/tests/auth/test_api.py:
        # so the API process gets a fresh engine when it boots.
        reset_engine()
        reset_sessionmaker()

    print("e2e seed complete:")
    print(f"  tenant_id          = {DEMO_TENANT_ID}")
    print(f"  agent_user_id      = {DEMO_AGENT_USER_ID}")
    print(f"  admin_user_id      = {DEMO_ADMIN_USER_ID}")
    print(f"  web_channel_id     = {DEMO_CHANNEL_ID}")
    print(f"  conversation_id    = {DEMO_CONVERSATION_ID}")
    print(f"  agent_email        = {DEMO_AGENT_EMAIL}")
    print(f"  password           = {DEMO_PASSWORD}")


if __name__ == "__main__":
    asyncio.run(main())
