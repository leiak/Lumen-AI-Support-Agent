# Tech Debt #16 — SES Tenant Reverse-Lookup

> **Status:** design approved (M3 kickoff). Implementation plan: `docs/superpowers/plans/2026-09-20-tech-debt-16-ses-tenant-reverse-lookup.md` (TBD).

## Goal

Stop trusting the `X-Tenant-ID` HTTP header for tenant resolution on the SES inbound webhook. Resolve tenant identity by reverse-looking-up the `Channel` row keyed on the inbound `to_address` (the company's own SES recipient address), which is globally unique per tenant.

## Context

`apps/api/src/main.py:email_inbound_webhook` (lines 219–283) currently:
1. Reads `X-Tenant-ID` header (line 243), falling back to `_settings.default_tenant_id`
2. Filters `Channel` query by both `tenant_id == header_value` AND `config_json["address"] == parsed.to_address`
3. Returns `dropped` if no Channel matches

Any attacker who can reach the webhook can spoof `X-Tenant-ID` to point at any tenant they like. The tenant identity is already implicit in `parsed.to_address` (the company's own SES recipient address) — each `Channel` row has a `tenant_id` FK plus a `config_json["address"]` JSONB. The right answer is to ignore the header entirely and ask the database "which tenant owns this recipient address?".

This is the cheapest correct fix. SNS subscription confirmation / SigV4 verification are heavier (require real AWS infrastructure in CI, can't be tested offline) and orthogonal — they can be layered on later without changing this code.

## Approach

**Reverse-lookup first, header second (header only for local dev fallback).** Specifically:
1. Query `Channel` by `to_address` + `ChannelType.EMAIL` only (no tenant filter)
2. If a Channel exists, take `channel.tenant_id` as the authoritative tenant
3. If no Channel exists, return `dropped` — the `X-Tenant-ID` header is **ignored** in production
4. Drop `default_tenant_id` fallback (it exists only to support local dev against `default_tenant_id`; production deploys never rely on it)

### File-level changes

#### `apps/api/src/main.py:email_inbound_webhook`

Replace the tenant-resolution block (lines 240–272) with:

```python
# Stage 16 / M3 / tech-debt #16: resolve tenant by reverse-looking-up
# the Channel row keyed on the inbound to_address. The X-Tenant-ID
# header is no longer trusted — it is logged only if present and
# differs from the resolved tenant (security audit signal).
channel = (
    await session.execute(
        select(Channel)
        .where(
            Channel.type == ChannelType.EMAIL,
            Channel.config_json["address"].astext == parsed.to_address,
        )
        .limit(1)
    )
).scalar_one_or_none()
if channel is None:
    logger.warning(
        "email.inbound.unknown_recipient",
        extra={"to_address": parsed.to_address},
    )
    return {"status": "dropped"}

tenant_id = channel.tenant_id

# Audit: if a header was sent AND it disagrees, surface that as a
# security signal — could be a misconfigured client or a probe.
header_tenant = request.headers.get("X-Tenant-ID")
if header_tenant and header_tenant != tenant_id:
    logger.warning(
        "email.inbound.header_tenant_mismatch",
        extra={
            "resolved_tenant_id": tenant_id,
            "header_tenant_id": header_tenant,
        },
    )
```

(The existing `email.inbound.unknown_recipient` log line stays at WARNING — `to_address` is the company's own recipient, not customer PII, so logging it for routing triage is fine. Per `M1` PII discipline this is acceptable.)

The downstream block (line 274 onwards) that calls `handle_email_inbound(tenant_id=tenant_id, ...)` stays unchanged.

Also:
- Remove `_settings.default_tenant_id` from the resolution path (still exists as a setting for legacy callers, but the webhook no longer reads it)
- Update the `KNOWN DEBT` comment block (lines 209–216) to point at this spec and say "fix shipped"
- Remove the README tech-debt #16 entry when the close-out PR lands

### Test changes

#### `apps/api/tests/channel/integration/conftest.py`

No change. The `sample_tenant` fixture already creates an ACTIVE EMAIL channel with `address="support@demo.test"`.

#### `apps/api/tests/channel/integration/test_email_inbound.py`

Existing tests that pass `headers={"X-Tenant-ID": tenant.id}` continue to work (the header is now optional). Update tests to:
- Send the inbound payload without the header in the success path → verify routing still works (tenant is resolved from the channel's `to_address`)
- Send the inbound payload with a wrong `X-Tenant-ID` header → verify routing still uses the channel's tenant (the header is ignored)
- Add `test_header_tenant_mismatch_logs_warning` — patch `logger.warning` and assert it's called with `email.inbound.header_tenant_mismatch`

Add new test cases:
- `test_tenant_resolved_from_to_address_not_header` — happy path with no header
- `test_wrong_header_does_not_affect_routing` — wrong X-Tenant-ID still resolves correct tenant
- `test_unknown_to_address_returns_dropped` — payload with `to: "unknown@foo.test"` returns `dropped` even with valid header
- `test_duplicate_message_id_returns_duplicate` — same `message_id` twice returns `duplicate` on the second call

### Migration of existing fixtures

If any existing test passes only `X-Tenant-ID` without a matching Channel row, that test is now broken. Audit the test file and remove stale header-only setups. (The `sample_tenant` fixture already creates a Channel, so this should be a no-op.)

## Security properties preserved

- **Anti-enumeration:** Channel lookup keyed only on `to_address` (globally unique per-tenant) — no enumeration signal leaked.
- **PII:** `to_address` is the company's own SES recipient (the `support@demo.test` style), not customer PII — safe to log.
- **Always-200:** the 4 outcomes (`ok`, `duplicate`, `dropped`, malformed) all return 200. SES never sees a 5xx.
- **Idempotency:** unchanged — `_check_existing_message` on `email_message_id_header` still gates retries.

## Out of scope

- SNS Subscription Confirmation (heavier; separate spec if/when SES-→-SNS wiring is added)
- SigV4 verification on the raw body (orthogonal layer, no current value)
- Per-tenant outbound reply validation (already covered by tenant_id on conversation row)

## Acceptance

- [ ] Webhook never reads `X-Tenant-ID` to compute `tenant_id`.
- [ ] Wrong / missing `X-Tenant-ID` header still routes correctly via `to_address` → Channel lookup.
- [ ] Disagreement between header and resolved tenant is logged as a WARNING (audit signal).
- [ ] All existing email inbound tests pass with `X-Tenant-ID` removed from the happy-path request.
- [ ] `_settings.default_tenant_id` is no longer referenced from `email_inbound_webhook`.
