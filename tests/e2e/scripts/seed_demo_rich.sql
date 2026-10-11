-- =====================================================================
-- Lumen AI — demo rich seed SQL reference
-- =====================================================================
-- This file documents the raw SQL emitted by
-- tests/e2e/scripts/seed_demo_rich.py when it inserts the 4 SLA
-- policies + 3 tickets + ticket-event chains + the conversation
-- ticket_id backfill.
--
-- Two flavors are kept side-by-side:
--   1) SQLAlchemy text() templates (the :name placeholders are bound
--      at runtime by Python — this is what the seed actually runs).
--   2) psql-runnable examples (the `-- $VARIABLE` lines are meant to be
--      substituted by the operator before pasting into psql; useful
--      when you want to insert a single ticket row by hand without
--      running the full Python seed).
--
-- Why raw SQL at all? Ticket SAEnum columns (``priority``, ``status``)
-- emit ``Enum.name`` (UPPERCASE) by default when ``values_callable`` is
-- unset. The PG ENUM types in this DB store ``lowercase`` values, so
-- the ORM raises ``invalid input value for enum ticket_priority:
-- "P0"``. Bypassing via text() writes literal lowercase strings
-- directly. The clean fix is to add
-- ``values_callable=lambda x: [e.value for e in x]`` to the SAEnum
-- declarations in ``apps/api/src/ticket/models.py``; the SQL keeps
-- working unchanged either way.
-- =====================================================================


-- ─── sla_policies INSERT ─────────────────────────────────────────────
-- Template (loaded by seed_demo_rich._seed_sla_policies):
INSERT INTO sla_policies (id, tenant_id, name, priority, first_response_minutes, resolution_minutes, business_hours_only, created_at)
VALUES (:id, :tenant_id, :name, :priority, :fr_min, :res_min, :bh_only, :created_at);

-- psql example (paste after substituting the variables):
--
--   INSERT INTO sla_policies (
--     id, tenant_id, name, priority,
--     first_response_minutes, resolution_minutes,
--     business_hours_only, created_at
--   ) VALUES (
--     '01HZDEMOSLAP0P1P0P0OUTAGE',
--     '01HZDEMO00000000000000000',
--     'P0 outage',
--     'P0',
--     15, 60,
--     FALSE,
--     NOW()
--   );


-- ─── tickets INSERT ─────────────────────────────────────────────────
-- Template (loaded by seed_demo_rich._seed_tickets):
INSERT INTO tickets (
    id, tenant_id, conversation_id, subject,
    category, priority, status, assignee_agent_id, sla_policy_id,
    sla_deadline_at, first_response_at, resolved_at, closed_at,
    created_at, updated_at
) VALUES (
    :id, :tid, :cid, :subject,
    :category, :priority, :status, :assignee, :sla_id,
    :sla_deadline, :first_resp, :resolved, :closed,
    :created_at, :updated_at
);

-- psql example (paste after substituting the variables):
--
--   INSERT INTO tickets (
--     id, tenant_id, conversation_id, subject,
--     category, priority, status, assignee_agent_id, sla_policy_id,
--     sla_deadline_at, first_response_at, resolved_at, closed_at,
--     created_at, updated_at
--   ) VALUES (
--     '01HZDEMOT0P0OUTAGE0000000',
--     '01HZDEMO00000000000000000',
--     '01HZDEMOCONV01000000000TECHNICAL',
--     'Production API returning 503s intermittently',
--     'outage', 'P0', 'in_progress',
--     '01HZDEMO00000000000000001',
--     '01HZDEMOSLAP0P1P0P0OUTAGE',
--     NOW() + INTERVAL '15 minutes',
--     NOW() - INTERVAL '10 minutes',
--     NULL, NULL,
--     NOW(), NOW()
--   );


-- ─── conversations.ticket_id UPDATE ──────────────────────────────────
-- Backfill the conversation's ticket_id pointer. Idempotent because
-- the WHERE includes ``ticket_id IS NULL``.

UPDATE conversations SET ticket_id = :tid
WHERE id = :cid AND ticket_id IS NULL;


-- ─── ticket_events INSERT ────────────────────────────────────────────
-- Template (loaded by seed_demo_rich._seed_tickets for each event):
INSERT INTO ticket_events (
    id, tenant_id, ticket_id,
    actor_type, actor_id, event_type, payload, created_at
) VALUES (
    :id, :tid, :ticket,
    :atype, :aid, :etype,
    CAST(:payload AS jsonb), :ts
);

-- psql example:
--
--   INSERT INTO ticket_events (
--     id, tenant_id, ticket_id,
--     actor_type, actor_id, event_type, payload, created_at
--   ) VALUES (
--     gen_random_uuid()::text,
--     '01HZDEMO00000000000000000',
--     '01HZDEMOT0P0OUTAGE0000000',
--     'system', NULL, 'ticket_created',
--     '{}'::jsonb,
--     NOW()
--   );


-- =====================================================================
-- Running the rich seed end-to-end
-- =====================================================================
--
-- Always prefer the Python entrypoint — it ensures the ticket IDs
-- match what tests/e2e/scripts/seed.py and the e2e fixtures expect:
--
--   cd apps/api && uv run python ../../tests/e2e/scripts/seed_demo_rich.py
--
-- Or, idempotent and one-shot, via scripts/bring-up.sh which runs all
-- 4 seeds in dependency order.
--
-- Manual psql flow (rare; useful when investigating a single bad row):
--
--   psql "$DATABASE_URL" \
--     -v ON_ERROR_STOP=1 \
--     -f tests/e2e/scripts/seed_demo_rich.sql
--
-- but note the SQL above uses :name placeholders, so psql will reject
-- them as-is — substitute each :name with the literal value from
-- tests/e2e/scripts/seed_demo_rich.py first (the tables at the top of
-- that file list the deterministic IDs).
-- =====================================================================