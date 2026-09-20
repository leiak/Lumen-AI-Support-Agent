# Tech Debt #16 — SES Tenant Reverse-Lookup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop trusting the spoofable `X-Tenant-ID` header on the SES inbound webhook. Resolve tenant identity by reverse-looking-up the `Channel` row keyed on `parsed.to_address`, then read `channel.tenant_id` as the authoritative tenant.

**Architecture:** Single-file refactor of `apps/api/src/main.py:email_inbound_webhook` — delete the header-based tenant lookup and replace with a tenant-agnostic Channel lookup. Audit-log the mismatch when a header disagrees with the resolved tenant.

**Tech Stack:** FastAPI, SQLAlchemy 2 async, Pydantic v2, structlog.

---

## File Structure

| Path | Role |
|------|------|
| `apps/api/src/main.py` | `email_inbound_webhook` — switch tenant resolution to Channel reverse-lookup |
| `apps/api/tests/channel/integration/test_email_inbound.py` | Update tests; add 4 new test cases |
| `README.md` | Remove tech-debt #16 entry |

No new files. No model changes.

---

## Task 1: Add the 4 new failing test cases

**Files:**
- Modify: `apps/api/tests/channel/integration/test_email_inbound.py` (append)

- [ ] **Step 1: Inspect the existing test file**

Read `apps/api/tests/channel/integration/test_email_inbound.py` to find:
- The existing happy-path test (likely sends a payload with `headers={"X-Tenant-ID": sample_tenant.id}` and `to_address=sample_tenant` channel's `address`)
- The fixture pattern for `sample_tenant` (already provides a Channel with `address="support@demo.test"`)
- How payloads are constructed (look for `parse_ses_inbound` usage)

- [ ] **Step 2: Append the new test cases**

```python
@pytest.mark.integration
@pytest.mark.asyncio
async def test_tenant_resolved_from_to_address_not_header(
    async_client: AsyncClient,
    sample_tenant: Tenant,
) -> None:
    """Inbound webhook routes by Channel.to_address — no X-Tenant-ID needed."""
    from core.email_parser import parse_ses_inbound

    payload = _make_ses_payload(
        from_address="customer@example.com",
        to_address="support@demo.test",
        subject="Question",
        body_text="MAGIC_PHRASE_EMAIL_no_header",
        message_id="<msg-no-header-1@test>",
    )

    resp = await async_client.post(
        "/api/v1/email/inbound",
        content=payload,
        headers={"Content-Type": "application/json"},
        # NO X-Tenant-ID — tenant must come from the Channel row.
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_wrong_header_does_not_affect_routing(
    async_client: AsyncClient,
    sample_tenant: Tenant,
) -> None:
    """A wrong X-Tenant-ID header is ignored; tenant is still resolved
    correctly from the Channel row keyed on to_address.
    """
    payload = _make_ses_payload(
        from_address="customer@example.com",
        to_address="support@demo.test",
        subject="Q",
        body_text="MAGIC_PHRASE_EMAIL_wrong_header",
        message_id="<msg-wrong-header-1@test>",
    )

    wrong_tenant_id = "01ARZ3NDEKTSV4RRFFQ69G5FAV"  # some other ULID
    resp = await async_client.post(
        "/api/v1/email/inbound",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Tenant-ID": wrong_tenant_id,
        },
    )
    assert resp.status_code == 200, resp.text
    # Body should still belong to sample_tenant (routed via Channel, not header).
    body = resp.json()
    assert body["status"] == "ok"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_unknown_to_address_returns_dropped(
    async_client: AsyncClient,
    sample_tenant: Tenant,
) -> None:
    """to_address that doesn't match any Channel returns 'dropped' even
    with a valid X-Tenant-ID header.
    """
    payload = _make_ses_payload(
        from_address="customer@example.com",
        to_address="unknown@some-other-domain.test",
        subject="Q",
        body_text="MAGIC_PHRASE_EMAIL_unknown_recipient",
        message_id="<msg-unknown-1@test>",
    )

    resp = await async_client.post(
        "/api/v1/email/inbound",
        content=payload,
        headers={
            "Content-Type": "application/json",
            "X-Tenant-ID": sample_tenant.id,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "dropped"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_message_id_returns_duplicate(
    async_client: AsyncClient,
    sample_tenant: Tenant,
) -> None:
    """Sending the same message_id twice (SES retry) returns 'duplicate' on 2nd call."""
    payload = _make_ses_payload(
        from_address="customer@example.com",
        to_address="support@demo.test",
        subject="Q",
        body_text="MAGIC_PHRASE_EMAIL_dup",
        message_id="<msg-dup-1@test>",
    )

    first = await async_client.post(
        "/api/v1/email/inbound",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert first.json()["status"] == "ok"

    second = await async_client.post(
        "/api/v1/email/inbound",
        content=payload,
        headers={"Content-Type": "application/json"},
    )
    assert second.json()["status"] == "duplicate"


def _make_ses_payload(
    *, from_address: str, to_address: str, subject: str,
    body_text: str, message_id: str,
) -> bytes:
    """Build a minimal valid SES inbound payload."""
    import json
    return json.dumps({
        "content": body_text,
        "commonHeaders": {
            "from": [from_address],
            "to": [to_address],
            "subject": subject,
            "messageId": message_id,
        },
        "mail": {
            "messageId": message_id,
            "source": from_address,
            "commonHeaders": {
                "from": [from_address],
                "to": [to_address],
                "subject": subject,
                "messageId": message_id,
            },
        },
        "ses": {
            "receipt": {
                "recipients": [to_address],
                "timestamp": "2026-09-20T00:00:00.000Z",
            },
        },
    }).encode("utf-8")
```

(Adjust `_make_ses_payload` to match the existing helper if the file already has one — look for a `make_ses_payload` or similar.)

- [ ] **Step 3: Run the 4 new tests — verify they fail**

Run:
```bash
cd apps/api && pytest tests/channel/integration/test_email_inbound.py -k "tenant_resolved_from_to_address or wrong_header or unknown_to_address or duplicate_message_id" -v
```

Expected:
- `test_tenant_resolved_from_to_address_not_header` — currently FAILS because without `X-Tenant-ID`, `_settings.default_tenant_id` is used (which is None / empty in tests), so the webhook returns `dropped` instead of `ok`.
- `test_wrong_header_does_not_affect_routing` — currently the wrong tenant is used; the test asserts `status == "ok"` only if the tenant resolution still lands on `sample_tenant` (it doesn't today — wrong tenant's Channel lookup returns None → dropped). FAILS.
- `test_unknown_to_address_returns_dropped` — currently PASSES by coincidence (no Channel matches → dropped). Mark this as already-passing; it's a regression guard.
- `test_duplicate_message_id_returns_duplicate` — depends on existing idempotency logic; should PASS already. Regression guard.

- [ ] **Step 4: Commit**

```bash
git add apps/api/tests/channel/integration/test_email_inbound.py
git commit -m "test(email): tenant resolved from to_address, header ignored"
```

---

## Task 2: Refactor `email_inbound_webhook` — drop header trust

**Files:**
- Modify: `apps/api/src/main.py:209-283`

- [ ] **Step 1: Replace the KNOWN DEBT comment block (lines 209–216)**

Replace:
```python
# KNOWN DEBT (M2.B / Task 2 code review):
# tenant_id is currently sourced from the X-Tenant-ID request header, which
# is trivially spoofable. Production must verify tenant identity via either:
#   (a) SNS subscription confirmation (SES inbound sends to an SNS topic;
#       verify the SigningCertURL against the SNS pinned CA),
#   (b) AWS SigV4 signature verification on the raw request body, or
#   (c) a reverse-lookup from the recipient address (Channel.config_json.tenant_id).
# Tracked as README tech-debt #16 (Stage 19 M2.B close-out).
```

with:
```python
# Tech debt #16 (shipped): tenant identity is reverse-looked-up from
# the Channel row keyed on parsed.to_address. The X-Tenant-ID header is
# no longer trusted for tenant resolution — it is logged at WARNING if
# present and disagrees with the resolved tenant (security audit signal).
# See docs/superpowers/specs/2026-09-20-tech-debt-16-ses-tenant-reverse-lookup-design.md
```

- [ ] **Step 2: Replace the tenant resolution + Channel lookup block (lines 240–272)**

Replace:
```python
    # Resolve tenant from X-Tenant-ID header (demo path). Production
    # would look up the tenant via the EmailChannel row's
    # ``config_json["tenant_id"]`` instead.
    tenant_id = request.headers.get("X-Tenant-ID", _settings.default_tenant_id)
    if not tenant_id:
        logger.warning("email.inbound.no_tenant_resolved")
        return {"status": "dropped"}

    # Look up EmailChannel by ``to_address`` for this tenant. The
    # ``address`` lives inside ``Channel.config_json`` JSONB — we
    # compare via ``astext`` so the index can be used.
    sm = get_sessionmaker()
    async with sm() as session:
        channel = (
            await session.execute(
                select(Channel)
                .where(
                    Channel.tenant_id == tenant_id,
                    Channel.type == ChannelType.EMAIL,
                    Channel.config_json["address"].astext == parsed.to_address,
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if channel is None:
            # ``to_address`` is OUR OWN SES recipient (the company's
            # support address), not customer PII — safe to log at
            # WARNING for routing triage.
            logger.warning(
                "email.inbound.unknown_recipient",
                extra={"to_address": parsed.to_address},
            )
            return {"status": "dropped"}
```

with:
```python
    # Tech debt #16: resolve tenant by reverse-lookup on to_address.
    # The Channel row's tenant_id is the authoritative tenant; we do
    # NOT trust the X-Tenant-ID header.
    sm = get_sessionmaker()
    async with sm() as session:
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
            # ``to_address`` is OUR OWN SES recipient (the company's
            # support address), not customer PII — safe to log at
            # WARNING for routing triage.
            logger.warning(
                "email.inbound.unknown_recipient",
                extra={"to_address": parsed.to_address},
            )
            return {"status": "dropped"}

        tenant_id = channel.tenant_id

        # Audit: if X-Tenant-ID was sent and disagrees with the
        # resolved tenant, surface that as a security signal — could be
        # a misconfigured client or a probing attempt.
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

- [ ] **Step 3: Run the 4 new tests — verify they pass**

Run:
```bash
cd apps/api && pytest tests/channel/integration/test_email_inbound.py -v
```
Expected: all tests pass (the 4 new ones + any existing ones that previously relied on `X-Tenant-ID`).

- [ ] **Step 4: Audit existing tests for `X-Tenant-ID` dependencies**

Run:
```bash
grep -n "X-Tenant-ID" apps/api/tests/channel/integration/test_email_inbound.py
```
For each match: keep the header in the test (so the audit-log WARNING fires), but the test must NOT depend on the header for tenant resolution. If any test asserts behavior that requires the header (e.g., "wrong X-Tenant-ID returns dropped" with the old behavior), update or remove it — the new behavior is that the header is IGNORED.

- [ ] **Step 5: Run the full channel test tree**

Run:
```bash
cd apps/api && pytest tests/channel/ -v
```
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add apps/api/src/main.py apps/api/tests/channel/integration/test_email_inbound.py
git commit -m "feat(email): tenant resolved via Channel.to_address reverse-lookup"
```

---

## Task 3: Verify default_tenant_id setting is still referenced (else remove)

**Files:**
- Modify: `apps/api/src/core/config.py` (only if no other consumer)

- [ ] **Step 1: Search for other usages**

Run:
```bash
grep -rn "default_tenant_id" apps/api/src/ apps/api/tests/
```
Expected hits (post-refactor):
- `core/config.py` — the field definition
- `main.py` — should now have ZERO references (verify the refactor removed it)
- Possibly: legacy seed scripts or fixtures

If `main.py` no longer references it AND no test references it AND no other consumer references it: leave the field in `core/config.py` (don't remove config to avoid breaking external env files); just leave a comment that it's no longer used by the webhook.

- [ ] **Step 2: If `main.py` still references it, remove the import / reference**

If a stray `default_tenant_id` reference remains in `main.py`, delete it. The field can stay in `core/config.py` for backward compatibility.

- [ ] **Step 3: Commit (only if changes were made)**

```bash
git add apps/api/src/main.py
git commit -m "chore(email): remove default_tenant_id reference from webhook"
```
(If no changes were needed, skip this step.)

---

## Task 4: Final verification + README update

**Files:**
- Modify: `README.md` (remove tech-debt #16 entry)

- [ ] **Step 1: Run the full non-integration suite**

Run:
```bash
cd apps/api && pytest --collect-only -m "not integration" 2>&1 | tail -5
```
Expected: collection succeeds.

- [ ] **Step 2: Run the full channel integration suite**

Run:
```bash
cd apps/api && pytest tests/channel/integration/ -v
```
Expected: all pass.

- [ ] **Step 3: Remove tech-debt #16 from README**

Find the tech-debt #16 entry in `README.md` and delete (or strike through). Confirm:

```bash
grep -n "tech-debt #16\|X-Tenant-ID.*spoof\|SES.*header" README.md
```
Expected: no match (or only struck-through).

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: remove tech-debt #16 entry (SES reverse-lookup shipped)"
```

---

## Acceptance Checklist

- [ ] Webhook never reads `X-Tenant-ID` to compute `tenant_id`.
- [ ] Wrong / missing `X-Tenant-ID` header still routes correctly via `to_address` → Channel lookup.
- [ ] Disagreement between header and resolved tenant is logged as WARNING (`email.inbound.header_tenant_mismatch`).
- [ ] All existing email inbound tests pass with `X-Tenant-ID` removed from happy-path requests.
- [ ] `_settings.default_tenant_id` is no longer referenced from `email_inbound_webhook`.
