"""Rich demo seed — extends ``seed.py`` with realistic CS data.

Idempotent. Run from inside the docker stack via::

    docker compose exec -T -e API_SRC_DIR=/app/src api python -u /tmp/seed_demo_rich.py

(after copying the file in with ``docker cp``).

What it adds on top of the e2e seed:

* 4 SLA policies (one per priority bucket) — admin SLA dashboard shows them
* 5 conversations across PENDING / OPEN / CLOSED with multi-message dialogs
* 3 tickets spanning NEW / IN_PROGRESS / RESOLVED across priorities
* Ticket audit events (new → triaged → in_progress → resolved chain)
* Budget usage rows for the current period so the budget UI is non-empty

The full e2e seed (``seed.py``) MUST have run first — this script
re-uses its tenant / channel / agent / admin ids so login still works.

Same PII discipline: customer text is fabricated generic CS questions,
never anything real; tenant + user ids are opaque ULIDs.

NOTE on raw SQL: this script writes to ``sla_policies``, ``tickets``,
and ``ticket_events`` via ``session.execute(text(...))`` instead of the
ORM. The model's ``priority`` and ``status`` columns are Postgres
ENUMs bound via SQLAlchemy SAEnum, and SAEnum in this project emits
``Enum.name`` (uppercase) instead of ``Enum.value`` (lowercase) when
``values_callable`` is unset. The alembic migration created the
underlying PG types with lowercase values, so the ORM insert raises
``invalid input value for enum ticket_priority: "P0"``. Bypassing via
text() sends literal lowercase strings directly.

The real fix is to add ``values_callable=lambda x: [e.value for e in x]``
to the SAEnum declarations in ``apps/api/src/ticket/models.py`` — that
is a separate hotfix; the seed must work today for self-testing.
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _find_repo_root(start: Path) -> Path:
    """Locate the repo root.

    Marker: directory containing ``apps/api/src``. ``tests/e2e`` is NOT
    required — when this seed runs inside the api container the repo
    root is ``/app`` (which only has ``src`` mounted). When run on the
    host, ``REPO_ROOT`` defaults to the marker-bearing parent of this
    file.
    """
    override = os.environ.get("REPO_ROOT")
    if override:
        p = Path(override).resolve()
        if (p / "apps" / "api" / "src").is_dir():
            return p
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "apps" / "api" / "src").is_dir():
            return candidate
    raise RuntimeError(
        f"could not locate repo root from {start} — "
        "set REPO_ROOT=... (the parent of apps/api/src)"
    )


_API_SRC = os.environ.get("API_SRC_DIR") or str(
    _find_repo_root(Path(__file__)) / "apps" / "api" / "src"
)
if _API_SRC not in sys.path:
    sys.path.insert(0, _API_SRC)

# Import everything the rich seed needs.  ticket.models must come before
# conversation.models so ``conversations.ticket_id`` FK resolves cleanly.
from ticket import models as _ticket_models  # noqa: E402,F401
from budget.models import (  # noqa: E402
    TenantBudget,
    TenantBudgetCredit,
)
from channel.enums import ChannelStatus, ChannelType  # noqa: E402
from channel.models import Channel  # noqa: E402
from conversation.enums import ConversationStatus, MessageRole  # noqa: E402
from conversation.models import Conversation, Message  # noqa: E402
from core.database import get_session, reset_engine, reset_sessionmaker  # noqa: E402
from core.id_gen import new_id  # noqa: E402
from llm_client.models import LLMUsage  # noqa: E402
from sqlalchemy import text  # noqa: E402
from tenant.enums import TenantPlan, TenantStatus, UserRole  # noqa: E402
from tenant.models import Tenant, User  # noqa: E402


# ─── Constants aligned with seed.py ──────────────────────────────────────
DEMO_TENANT_ID = "01HZDEMO00000000000000000"
DEMO_AGENT_USER_ID = "01HZDEMO00000000000000001"
DEMO_ADMIN_USER_ID = "01HZDEMO00000000000000002"
DEMO_CHANNEL_ID = "01HZDEMO00000000000000003"
DEMO_CONVERSATION_ID = "01HZDEMO00000000000000004"

# Extra channel for variety (a second web channel, different name)
DEMO_CHANNEL_2_ID = "01HZDEMOC20CHAN000000000"

# Fixed conversation + ticket ids (deterministic, easy to reference)
C_OPEN_BILLING = "01HZDEMOC0OPEN0BILLING00"        # PENDING, no agent
C_OPEN_TECHNICAL = "01HZDEMOC0OPEN0TECH0NIC00"     # OPEN, agent claimed
C_OPEN_LOGIN_ISSUE = "01HZDEMOC0OPENLOGINIS00SUE"  # OPEN, AI-handling
C_CLOSED_REFUND = "01HZDEMOC0CLOSEDREFUND0ABC"     # CLOSED, resolved
C_OPEN_FEATURE_REQ = "01HZDEMOC0FEATURE0REQ0ST00"  # PENDING low pri

# Ticket ids (one per priority, varied status)
T_P0_OUTAGE = "01HZDEMOT0P0OUTAGE0TICKET"
T_P1_BILLING = "01HZDEMOT0P1BILLING0TICKET"
T_P2_HOWTO = "01HZDEMOT0P2HOWTO0000TKT"

# SLA policy ids (one per priority)
SLA_P0 = "01HZDEMOSLAP000ID0000000"
SLA_P1 = "01HZDEMOSLAP100ID0000000"
SLA_P2 = "01HZDEMOSLAP200ID0000000"
SLA_P3 = "01HZDEMOSLAP300ID0000000"


def _ago(minutes: int) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


# ─── SLA policies (one per priority) ─────────────────────────────────────
async def _seed_sla_policies(session) -> int:
    """4 policies — admin SLA dashboard renders one row per priority.

    Uses raw SQL because ``sla_policies.priority`` is a PG ENUM
    (``ticket_priority``) and SAEnum sends uppercase names by default;
    see module docstring. The values here are the lowercase
    ``Enum.value`` strings.
    """
    rows = [
        (SLA_P0, "P0", "P0 Critical — 15min / 4h", 15, 240),
        (SLA_P1, "P1", "P1 High — 1h / 8h", 60, 480),
        (SLA_P2, "P2", "P2 Normal — 4h / 1d", 240, 1440),
        (SLA_P3, "P3", "P3 Low — 1d / 3d", 1440, 4320),
    ]
    inserted = 0
    for sid, priority, name, first, resolve in rows:
        existing = await session.execute(
            text("SELECT 1 FROM sla_policies WHERE id = :id"),
            {"id": sid},
        )
        if existing.scalar() is not None:
            continue
        await session.execute(
            text(
                "INSERT INTO sla_policies (id, tenant_id, name, priority, "
                "first_response_minutes, resolution_minutes, "
                "business_hours_only, created_at) "
                "VALUES (:id, :tid, :name, :priority, :first, :resolve, "
                "false, now())"
            ),
            {
                "id": sid,
                "tid": DEMO_TENANT_ID,
                "name": name,
                "priority": priority,
                "first": first,
                "resolve": resolve,
            },
        )
        inserted += 1
    return inserted


# ─── Channels ────────────────────────────────────────────────────────────
async def _seed_extra_channel(session) -> int:
    if await session.get(Channel, DEMO_CHANNEL_2_ID) is not None:
        return 0
    session.add(
        Channel(
            id=DEMO_CHANNEL_2_ID,
            tenant_id=DEMO_TENANT_ID,
            type=ChannelType.WEB,
            name="demo-web-eu",
            credentials_encrypted='{"origin":"http://localhost:8088"}',
            status=ChannelStatus.ACTIVE,
            config_json={},
        )
    )
    await session.flush()
    return 1


# ─── Conversations + messages ────────────────────────────────────────────
CONVERSATIONS = [
    # (id, channel, customer_id, status, agent_assigned?, ai_handling, age_min,
    #  last_activity_age_min, messages: list of (role, content))
    (
        C_OPEN_BILLING,
        DEMO_CHANNEL_ID,
        "visitor-alpha-1",
        ConversationStatus.PENDING,
        None,
        False,
        45,
        45,
        [
            (MessageRole.CUSTOMER, "I was charged twice on my last invoice. Can someone refund the duplicate?"),
            (MessageRole.AI, "I've created ticket #P1-BILLING for you. The finance team reviews duplicates within 4 business hours."),
        ],
    ),
    (
        C_OPEN_TECHNICAL,
        DEMO_CHANNEL_ID,
        "visitor-beta-22",
        ConversationStatus.OPEN,
        DEMO_AGENT_USER_ID,
        True,
        180,
        12,
        [
            (MessageRole.CUSTOMER, "Our SDK keeps dropping websocket connections after 90 seconds."),
            (MessageRole.AI, "Thanks for reaching out. Let me look that up for you."),
            (MessageRole.TOOL, "search_internal_kb returned 2 chunks (kb=websocket-stability)"),
            (MessageRole.AI, "This is a known issue with idle timeout. Try setting keepAliveIntervalMs to 30000."),
            (MessageRole.CUSTOMER, "Tried that — same drops. Can I escalate?"),
            (MessageRole.AGENT, "I just claimed this. Sharing logs in a sec — pulling them now."),
            (MessageRole.CUSTOMER, "Sure, also getting 503 on the /v1/messages endpoint every ~3 minutes."),
            (MessageRole.AGENT, "Got the logs. The 503 is from rate-limit fallback — let me check your tenant config."),
        ],
    ),
    (
        C_OPEN_LOGIN_ISSUE,
        DEMO_CHANNEL_ID,
        "visitor-gamma-7",
        ConversationStatus.OPEN,
        None,
        True,
        7,
        1,
        [
            (MessageRole.CUSTOMER, "I can't log in since this morning — getting 'invalid credentials' but the password is right."),
            (MessageRole.AI, "Sorry to hear that. A few quick questions: are you on the staging or production tenant? Have you tried resetting your password?"),
            (MessageRole.CUSTOMER, "Production. Password reset email hasn't arrived yet."),
        ],
    ),
    (
        C_CLOSED_REFUND,
        DEMO_CHANNEL_ID,
        "visitor-delta-3",
        ConversationStatus.CLOSED,
        DEMO_AGENT_USER_ID,
        False,
        60 * 24 * 3,  # 3 days ago
        60 * 24 * 2,  # 2 days ago (last reply)
        [
            (MessageRole.CUSTOMER, "Refunded me twice for the same month."),  # not PII
            (MessageRole.AI, "Sorry for the trouble — creating a refund ticket."),
            (MessageRole.AGENT, "Confirmed the duplicate. Refunded both, finance ticket INTERNAL-7821 attached."),
            (MessageRole.CUSTOMER, "Thanks, all settled."),
            (MessageRole.AGENT, "Closing this. Let us know if anything else comes up."),
        ],
    ),
    (
        C_OPEN_FEATURE_REQ,
        DEMO_CHANNEL_ID,
        "visitor-epsilon-99",
        ConversationStatus.PENDING,
        None,
        False,
        600,
        600,
        [
            (MessageRole.CUSTOMER, "Feature request: support for Twilio SMS as an inbound channel. Our agents live in there."),
            (MessageRole.AI, "Thanks — passed to product. Want me to notify you by email when there's an update?"),
        ],
    ),
]


async def _seed_conversations(session) -> int:
    inserted = 0
    for row in CONVERSATIONS:
        (cid, channel_id, cust_id, status, assigned,
         ai, age_min, last_act_min, messages) = row
        if await session.get(Conversation, cid) is not None:
            continue
        session.add(
            Conversation(
                id=cid,
                tenant_id=DEMO_TENANT_ID,
                channel_id=channel_id,
                customer_external_id=cust_id,
                status=status,
                assigned_agent_id=assigned,
                ai_handling=ai,
                opened_at=_ago(age_min),
                last_activity_at=_ago(last_act_min),
            )
        )
        await session.flush()
        # Stagger messages across the conversation's lifetime so the
        # timeline renders chronologically
        span = max(age_min - last_act_min, 1)
        if age_min > 60:
            message_times = [
                _ago(age_min - i * span // max(len(messages), 1))
                for i in range(len(messages))
            ]
        else:
            message_times = [
                _ago(last_act_min + i * span // max(len(messages), 1))
                for i in range(len(messages))
            ]
        message_times = sorted(message_times)
        for (role, text_content), ts in zip(messages, message_times, strict=False):
            session.add(
                Message(
                    id=new_id(),
                    conversation_id=cid,
                    role=role,
                    content_text=text_content,
                    sender_id=None,
                    created_at=ts,
                )
            )
        await session.flush()
        inserted += 1
    return inserted


# ─── Tickets + ticket events (raw SQL — see module docstring) ────────────
async def _seed_tickets(session) -> int:
    """3 tickets across priorities + a few audit events each.

    Uses raw SQL to bypass SAEnum's uppercase name emission. Each
    ticket row + its events are written in one statement group per
    ticket; the conversation's ``ticket_id`` pointer is also updated.
    """
    inserted = 0
    tickets = [
        # P0 outage — in_progress, assigned, SLA counting down
        dict(
            tid=T_P0_OUTAGE,
            conv_id=C_OPEN_TECHNICAL,
            subject="Production API returning 503s intermittently",
            category="outage",
            priority="P0",
            status="in_progress",
            assignee=DEMO_AGENT_USER_ID,
            sla_id=SLA_P0,
            sla_offset_min=15,
            created_at=_ago(15),
            first_response_at=_ago(10),  # within 15min — green
            resolved_at=None,
            closed_at=None,
            events=[
                ("system", None, "ticket_created", {}),
                ("system", None, "ticket_triaged", {"to": "triaged"}),
                ("agent", DEMO_AGENT_USER_ID, "ticket_assigned",
                 {"to": "in_progress", "assignee_agent_id": DEMO_AGENT_USER_ID}),
                ("agent", DEMO_AGENT_USER_ID, "agent_replied",
                 {"content": "Pulling logs now, will revert in 15min"}),
            ],
        ),
        # P1 billing — waiting_customer, unassigned, slower SLA
        dict(
            tid=T_P1_BILLING,
            conv_id=C_OPEN_BILLING,
            subject="Duplicate charge on invoice #INV-2026-09",
            category="billing",
            priority="P1",
            status="waiting_customer",
            assignee=None,
            sla_id=SLA_P1,
            sla_offset_min=60,
            created_at=_ago(120),
            first_response_at=_ago(60),
            resolved_at=None,
            closed_at=None,
            events=[
                ("system", None, "ticket_created", {}),
                ("system", None, "ticket_triaged", {"to": "triaged"}),
                ("ai", None, "ticket_assigned",
                 {"to": "waiting_customer",
                  "reason": "Need customer to confirm transaction ID"}),
            ],
        ),
        # P2 howto — resolved 2d ago, agent replied, customer confirmed
        dict(
            tid=T_P2_HOWTO,
            conv_id=C_CLOSED_REFUND,
            subject="How to issue a partial refund",
            category="how-to",
            priority="P2",
            status="resolved",
            assignee=DEMO_AGENT_USER_ID,
            sla_id=SLA_P2,
            sla_offset_min=240,
            created_at=_ago(60 * 24 * 3),
            first_response_at=_ago(60 * 24 * 3 - 60),
            resolved_at=_ago(60 * 24 * 2),
            closed_at=None,
            events=[
                ("system", None, "ticket_created", {}),
                ("agent", DEMO_AGENT_USER_ID, "ticket_triaged",
                 {"to": "triaged"}),
                ("agent", DEMO_AGENT_USER_ID, "ticket_resolved",
                 {"to": "resolved",
                  "resolution_note": "Refund processed via API; closing ticket."}),
            ],
        ),
    ]

    for t in tickets:
        existing = await session.execute(
            text("SELECT 1 FROM tickets WHERE id = :id"),
            {"id": t["tid"]},
        )
        if existing.scalar() is not None:
            continue
        sla_deadline = t["created_at"] + timedelta(minutes=t["sla_offset_min"])
        await session.execute(
            text(
                "INSERT INTO tickets (id, tenant_id, conversation_id, subject, "
                "category, priority, status, assignee_agent_id, sla_policy_id, "
                "sla_deadline_at, first_response_at, resolved_at, closed_at, "
                "created_at, updated_at) "
                "VALUES (:id, :tid, :cid, :subject, :category, :priority, "
                ":status, :assignee, :sla_id, :sla_deadline, :first_resp, "
                ":resolved, :closed, :created_at, :updated_at)"
            ),
            {
                "id": t["tid"],
                "tid": DEMO_TENANT_ID,
                "cid": t["conv_id"],
                "subject": t["subject"],
                "category": t["category"],
                "priority": t["priority"],
                "status": t["status"],
                "assignee": t["assignee"],
                "sla_id": t["sla_id"],
                "sla_deadline": sla_deadline,
                "first_resp": t["first_response_at"],
                "resolved": t["resolved_at"],
                "closed": t["closed_at"],
                "created_at": t["created_at"],
                "updated_at": t["created_at"],
            },
        )
        # Backfill the conversation's ticket_id pointer
        await session.execute(
            text(
                "UPDATE conversations SET ticket_id = :tid "
                "WHERE id = :cid AND ticket_id IS NULL"
            ),
            {"tid": t["tid"], "cid": t["conv_id"]},
        )
        # Audit events (staggered 2min apart for readable timeline)
        ts = t["created_at"]
        for actor_type, actor_id, ev_type, payload in t["events"]:
            await session.execute(
                text(
                    "INSERT INTO ticket_events (id, tenant_id, ticket_id, "
                    "actor_type, actor_id, event_type, payload, created_at) "
                    "VALUES (:id, :tid, :ticket, :atype, :aid, :etype, "
                    "CAST(:payload AS jsonb), :ts)"
                ),
                {
                    "id": new_id(),
                    "tid": DEMO_TENANT_ID,
                    "ticket": t["tid"],
                    "atype": actor_type,
                    "aid": actor_id,
                    "etype": ev_type,
                    "payload": _json_dumps(payload),
                    "ts": ts,
                },
            )
            ts += timedelta(minutes=2)
        inserted += 1
    return inserted


def _json_dumps(obj: dict) -> str:
    """Tiny JSON encoder for the payload column. Avoids importing
    stdlib json at the top so the file stays self-contained.
    """
    import json
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# ─── Budget: extend usage so the dashboard isn't empty ──────────────────
async def _seed_budget_usage(session) -> int:
    """Add LLMUsage rows for the current month so the budget UI isn't empty.

    Hard cap is 20,000 tokens (set by ``seed_budget.py``). We add ~7,200
    tokens of usage across 6 days so the dashboard shows ~36% consumed.
    """
    inserted = 0
    rows = [
        (6, 800, 200, "claude-3-5-sonnet-20241022", "anthropic", C_OPEN_BILLING),
        (5, 1200, 400, "claude-3-5-sonnet-20241022", "anthropic", C_OPEN_TECHNICAL),
        (4, 600, 150, "claude-3-5-sonnet-20241022", "anthropic", C_OPEN_BILLING),
        (3, 1500, 500, "claude-3-5-sonnet-20241022", "anthropic", C_OPEN_TECHNICAL),
        (2, 800, 200, "claude-3-5-sonnet-20241022", "anthropic", DEMO_CONVERSATION_ID),
        (1, 1200, 350, "claude-3-5-sonnet-20241022", "anthropic", C_OPEN_TECHNICAL),
    ]
    for day_offset, prompt, completion, model, provider, conv_id in rows:
        # llm_usage.created_at is TIMESTAMP WITHOUT TIME ZONE (see
        # llm_client/models.py); asyncpg refuses tz-aware datetimes
        # against that type. Strip tzinfo to match the column.
        ts = (datetime.now(UTC) - timedelta(days=day_offset, hours=2)).replace(tzinfo=None)
        session.add(
            LLMUsage(
                id=new_id(),
                tenant_id=DEMO_TENANT_ID,
                provider=provider,
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost_usd=0.0,
                request_id=f"demo-seed-{day_offset}-{new_id()[:8]}",
                cached=False,
                metadata_json={
                    "conversation_id": conv_id,
                    "source": "demo-rich-seed",
                },
                created_at=ts,
            )
        )
        inserted += 1
    return inserted


async def main() -> None:
    try:
        async with get_session() as session:
            # Make sure tenant exists (e2e seed.py runs first, but be defensive)
            if await session.get(Tenant, DEMO_TENANT_ID) is None:
                session.add(
                    Tenant(
                        id=DEMO_TENANT_ID,
                        name="Acme Demo",
                        plan=TenantPlan.PRO,
                        status=TenantStatus.ACTIVE,
                        settings={"source": "rich-demo-seed"},
                    )
                )
                await session.flush()
            n_sla = await _seed_sla_policies(session)
            n_chan = await _seed_extra_channel(session)
            n_conv = await _seed_conversations(session)
            n_tix = await _seed_tickets(session)
            n_use = await _seed_budget_usage(session)
            await session.commit()
    finally:
        reset_engine()
        reset_sessionmaker()

    print("rich-demo seed complete:")
    print(f"  sla_policies   +{n_sla}")
    print(f"  channels       +{n_chan}")
    print(f"  conversations  +{n_conv}  (6 total now: 1 from seed.py + new)")
    print(f"  tickets        +{n_tix}   (1 per priority bucket)")
    print(f"  usage rows     +{n_use}  (~7200 tokens consumed this period)")


if __name__ == "__main__":
    asyncio.run(main())
