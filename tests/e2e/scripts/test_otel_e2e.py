"""End-to-end smoke for M6 OpenTelemetry tracing.

Drives the docker-compose stack (api + otel-collector + tempo) and
confirms:

1. otel-collector:4317 (gRPC) + tempo:33000 (HTTP) are reachable.
2. A real widget chat through the api container produces a trace
   in Tempo (captured via the X-Trace-Id response header from
   ``POST /api/v1/widget/token``).
3. The trace contains the expected span hierarchy:
   - 1 ``http.request`` root span
   - best-effort: ``llm.chat`` + outbound ``POST``/``anthropic`` httpx
     span (these land in a sibling trace produced by the widget WS
     path, not the same trace as the /token POST — see the comment
     in step 4 below).
4. No span attribute KEY contains a forbidden PII token. The check
   uses the whole-key-boundary regex from
   ``tests/observability/test_pii_whitelist.py``
   (``_is_forbidden_keyword_in_attribute_key``) — not substring
   search, so whitelisted keys like ``llm.tokens.input`` do not
   produce false positives.

Mirrors ``tests/e2e/scripts/test_tenant_llm_config_audit_e2e.py``:
argparse + ``[OK|WARN|FAIL|INFO]`` + exit codes 0/1/2.

Run from repo root with the docker stack up::

    DATABASE_URL=... JWT_SECRET=... ANTHROPIC_API_KEY=... \\
    python tests/e2e/scripts/test_otel_e2e.py \\
        --api-base http://localhost:8000
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

import requests
import websockets as ws_aio


# --- Repo-root + sys.path bootstrap -------------------------------------

def _find_repo_root(start: Path) -> Path:
    cur = start.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / "apps" / "api" / "src").is_dir() and (
            candidate / "tests" / "e2e"
        ).is_dir():
            return candidate
    raise RuntimeError("could not locate repo root")


_REPO_ROOT = _find_repo_root(Path(__file__))
_API_SRC = _REPO_ROOT / "apps" / "api" / "src"
if str(_API_SRC) not in sys.path:
    sys.path.insert(0, str(_API_SRC))


# Late imports — must come after sys.path tweak above.
from core.database import get_session, reset_engine, reset_sessionmaker  # noqa: E402
from core.id_gen import new_id  # noqa: E402
from channel.enums import ChannelStatus, ChannelType  # noqa: E402
from channel.models import Channel  # noqa: E402
from tenant.enums import TenantPlan, TenantStatus  # noqa: E402
from tenant.models import Tenant  # noqa: E402


# --- PII scan (whole-key-boundary) ---------------------------------------

# Mirrors PII_FORBIDDEN in apps/api/src/observability/tracing.py. The
# set is intentionally duplicated here (vs imported) so the e2e
# script remains a single file with no import surprises, and so the
# static-scan test in test_pii_whitelist.py continues to be the
# authoritative source for the code-base — this script only applies
# the same boundary regex at runtime against actual span attributes.
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


def _is_forbidden_keyword_in_attribute_key(attr_key: str, token: str) -> bool:
    """Match forbidden token only as a whole attribute key segment.

    This is the runtime version of the regex from
    ``tests/observability/test_pii_whitelist.py``. The token must
    appear as a whole attribute-key segment bounded by ``.``,
    ``"``, or start/end of string. Substring matches on whitelisted
    keys like ``llm.tokens.input`` are NOT flagged.

    Args:
        attr_key: The attribute key (e.g. ``"llm.tokens.input"``).
        token: The forbidden token (e.g. ``"token"``).
    """
    pattern = re.compile(
        rf'(?:^|[."])({re.escape(token)})(?:$|[."])'
    )
    return pattern.search(attr_key) is not None


def _check_pii_in_attributes(span_name: str, attrs: dict) -> str | None:
    """Return the first forbidden token found in any attribute key, or None."""
    for key in attrs:
        for token in PII_FORBIDDEN:
            if _is_forbidden_keyword_in_attribute_key(key, token):
                return token
    return None


# --- Tempo endpoints -----------------------------------------------------

TEMPO_HOST = "http://localhost:33000"
TEMPO_TRACE_URL = f"{TEMPO_HOST}/api/traces/{{trace_id}}"
TEMPO_SEARCH_URL = f"{TEMPO_HOST}/api/search"
TEMPO_UI_URL = f"{TEMPO_HOST}/trace/{{trace_id}}"


# --- Logging helpers -----------------------------------------------------

def _print(level: str, msg: str) -> None:
    line = f"  [{level}] {msg}"
    try:
        print(line)
    except UnicodeEncodeError:
        sys.stdout.buffer.write((line + "\n").encode("utf-8", errors="replace"))
        sys.stdout.buffer.flush()


# --- Step 1: infrastructure check ---------------------------------------

def _check_collector_up() -> bool:
    """otel-collector:4317 (gRPC) responds to a TCP connect."""
    import socket
    try:
        with socket.create_connection(("localhost", 4317), timeout=3):
            return True
    except OSError as exc:
        _print("FAIL", f"otel-collector:4317 unreachable: {exc}")
        return False


def _check_tempo_up() -> bool:
    try:
        r = requests.get(f"{TEMPO_HOST}/ready", timeout=3)
        return r.status_code == 200
    except requests.RequestException as exc:
        _print("FAIL", f"tempo:33000 unreachable: {type(exc).__name__}: {exc}")
        return False


def _step1_infra_check() -> bool:
    if not _check_collector_up():
        return False
    if not _check_tempo_up():
        return False
    _print("OK", "otel-collector:4317 + tempo:33000 reachable")
    return True


# --- Step 2: drive a widget chat (capture X-Trace-Id) -------------------

DEV_FERNET_KEY = "nLJ6W1cj0isqEsiRqrg98WJ9EPVB7e-BM1ub1TIRO2Y="


async def _seed_minimal_tenant() -> tuple[str, str]:
    """Insert a fresh tenant + WEB channel. BYOK is optional (the e2e
    will still drive a trace even if the LLM call returns a fallback —
    the trace wiring is what we verify, not the LLM response).
    """
    tenant_id = new_id()
    channel_id = new_id()
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or None
    async with get_session() as s:
        s.add(Tenant(
            id=tenant_id,
            name=f"otel-e2e-{uuid.uuid4().hex[:8]}",
            plan=TenantPlan.FREE, status=TenantStatus.ACTIVE, settings={},
        ))
        await s.flush()
        s.add(Channel(
            id=channel_id, tenant_id=tenant_id,
            type=ChannelType.WEB, name="otel-e2e-web",
            credentials_encrypted=json.dumps({"origin": "http://localhost:8080"}),
            status=ChannelStatus.ACTIVE, config_json={},
        ))
        if api_key:
            from llm_client.tenant_config_crypto import TenantLLMConfigCipher
            from llm_client.tenant_config_models import TenantLLMConfig
            cipher = TenantLLMConfigCipher(DEV_FERNET_KEY)
            s.add(TenantLLMConfig(
                id=new_id(), tenant_id=tenant_id,
                provider_name="anthropic",
                encrypted_api_key=cipher.encrypt(api_key),
                base_url=base_url, enabled=True,
            ))
        await s.commit()
    return tenant_id, channel_id


def _step2_drive_widget(api_base: str) -> tuple[str, str] | None:
    """Seed a tenant, POST /widget/token (captures X-Trace-Id T1),
    open the WS, send PONG, wait for the reply. Returns
    ``(trace_id, channel_id)`` or ``None`` on failure.
    """
    tenant_id, channel_id = asyncio.run(_seed_minimal_tenant())
    _print("OK", f"seeded tenant={tenant_id} channel={channel_id}")

    api_base = api_base.rstrip("/")
    try:
        r = requests.post(
            f"{api_base}/api/v1/widget/token",
            json={
                "channel_id": channel_id,
                "external_user_id": f"otel-e2e-{uuid.uuid4().hex[:6]}",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        _print("FAIL", f"POST /widget/token: {type(exc).__name__}: {exc}")
        return None
    if r.status_code != 200:
        _print("FAIL", f"POST /widget/token status={r.status_code} body={r.text[:200]!r}")
        return None
    token = r.json()["token"]
    trace_id = r.headers.get("X-Trace-Id")
    if not trace_id:
        _print("FAIL", "POST /widget/token did not echo X-Trace-Id")
        return None
    _print("OK", f"POST /widget/token returned X-Trace-Id={trace_id}")

    # Drive one WS round-trip so the LLM path produces a trace. We
    # don't strictly need the reply — the trace wiring is what the
    # e2e is checking. A failure here marks the round as WARN, not
    # FAIL (the HTTP trace from /token is the primary anchor).
    async def _drive() -> bool:
        ws_base = api_base.replace("http://", "ws://").replace("https://", "wss://")
        try:
            async with ws_aio.connect(
                f"{ws_base}/api/v1/widget/ws?token={token}",
                origin="http://localhost:8080",
                open_timeout=10,
            ) as ws:
                await ws.send(json.dumps({
                    "type": "message", "text": "Reply PONG.",
                }))
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=10)
                    except asyncio.TimeoutError:
                        break
                    try:
                        e = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    et = (e.get("type") or e.get("event") or "")
                    if et in ("message.complete", "assistant_message", "agent_message_chunk"):
                        return True
        except Exception as exc:
            _print("WARN", f"WS round-trip exception: {type(exc).__name__}: {exc}")
            return False
        _print("WARN", "WS round-trip timed out before any reply event")
        return False

    asyncio.run(_drive())
    return trace_id, channel_id


# --- Step 3: query Tempo for the trace ----------------------------------

def _step3_query_tempo(trace_id: str) -> list[dict] | None:
    """Poll Tempo for the trace. Returns the span list, or None.

    Tempo's API: ``GET /api/traces/{traceID}`` returns the trace
    directly (``{"traceID": ..., "spans": [...]}``). Span objects use
    camelCase: ``spanID``, ``parentSpanID``, ``name``, ``attributes``.
    """
    deadline = time.time() + 10
    last_status: int | None = None
    while time.time() < deadline:
        try:
            r = requests.get(TEMPO_TRACE_URL.format(trace_id=trace_id), timeout=3)
        except requests.RequestException:
            time.sleep(0.5)
            continue
        last_status = r.status_code
        if r.status_code == 200:
            data = r.json()
            spans = data.get("spans", [])
            if spans:
                _print("OK", f"Tempo returned {len(spans)} spans for trace {trace_id}")
                return spans
        time.sleep(0.5)
    _print(
        "FAIL",
        f"trace {trace_id} did not appear in Tempo within 10s (last status={last_status})",
    )
    return None


def _step3b_search_recent_llm_traces() -> str | None:
    """Best-effort: search Tempo for the most recent trace containing
    an ``llm.chat`` span. The widget WS path produces its own trace
    (the WS handshake is a separate span tree from the /token POST),
    so we cannot rely on the X-Trace-Id alone to find the LLM spans.
    Returns a trace_id (str) or None.
    """
    try:
        r = requests.get(
            TEMPO_SEARCH_URL,
            params={"tags": "name=llm.chat", "limit": 5},
            timeout=5,
        )
    except requests.RequestException as exc:
        _print("WARN", f"Tempo search failed: {type(exc).__name__}: {exc}")
        return None
    if r.status_code != 200:
        _print("WARN", f"Tempo search status={r.status_code} body={r.text[:200]!r}")
        return None
    try:
        body = r.json()
    except ValueError:
        _print("WARN", "Tempo search returned non-JSON body")
        return None
    traces = body.get("traces", [])
    if not traces:
        _print("WARN", "Tempo search returned 0 traces for name=llm.chat")
        return None
    # Pick the freshest one (Tempo's search returns startTime descending).
    return str(traces[0].get("traceID") or traces[0].get("traceId") or "")


# --- Step 4: assert hierarchy -------------------------------------------

def _step4_assert_span_hierarchy(spans: list[dict]) -> bool:
    """The HTTP trace must have an http.request root span.

    The LLM trace (if found) is checked separately by
    :func:`_step4b_assert_llm_hierarchy`.
    """
    names = {s.get("name", "") for s in spans}
    if "http.request" not in names:
        _print("FAIL", f"missing required span: http.request (have: {sorted(names)})")
        return False
    _print("OK", f"HTTP span hierarchy OK: {sorted(names)}")
    return True


def _step4b_assert_llm_hierarchy(llm_trace_id: str) -> bool:
    """Fetch the LLM trace and assert it has llm.chat + an outbound
    httpx span (POST or anthropic in the name).
    """
    spans = _step3_query_tempo(llm_trace_id)
    if spans is None:
        return False
    names = {s.get("name", "") for s in spans}
    if "llm.chat" not in names:
        _print("FAIL", f"LLM trace missing llm.chat span (have: {sorted(names)})")
        return False
    has_httpx = any(
        "POST" in n or "anthropic" in n or "openai" in n
        for n in names
    )
    if not has_httpx:
        _print("FAIL", f"no outbound httpx span in LLM trace; names={names}")
        return False
    _print("OK", f"LLM span hierarchy OK: {sorted(names)}")
    return True


# --- Step 5: PII scan (whole-key-boundary) ------------------------------

def _step5_assert_no_pii_in_spans(spans: list[dict]) -> bool:
    """No attribute KEY matches a forbidden PII token as a whole key
    segment. Values are NOT scanned (PII could be in a value by
    accident, but the static-scan test catches the code-side issue;
    runtime value scrubbing is the responsibility of the @traced
    attribute whitelist + the _log_attribute_not_whitelisted runtime
    guard).
    """
    for span in spans:
        attrs = span.get("attributes", {}) or {}
        forbidden = _check_pii_in_attributes(span.get("name", ""), attrs)
        if forbidden is not None:
            _print(
                "FAIL",
                f"span {span.get('name')!r} has forbidden attribute key token={forbidden!r}",
            )
            return False
    _print("OK", "no PII tokens in any span attribute KEY (whole-segment scan)")
    return True


# --- Step 6: orphan parent check ----------------------------------------

def _step6_assert_no_orphan_spans(spans: list[dict]) -> bool:
    """Every non-root span has a parent_span_id present in the trace."""
    span_ids = {s.get("spanID") for s in spans}
    for span in spans:
        parent = span.get("parentSpanID", "")
        if parent and parent not in span_ids:
            _print(
                "FAIL",
                f"span {span.get('spanID')} has orphan parent {parent}",
            )
            return False
    _print("OK", "all parent links valid (no orphan spans)")
    return True


# --- Main flow ----------------------------------------------------------

async def run(args: argparse.Namespace) -> int:
    api_base = args.api_base.rstrip("/")
    print("=" * 70)
    print("M6 OpenTelemetry tracing — E2E")
    print("=" * 70)
    print(f"  api_base = {api_base}")
    print(f"  tempo    = {TEMPO_HOST}")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        _print(
            "WARN",
            "ANTHROPIC_API_KEY not set — WS round-trip will likely get a "
            "fallback; trace wiring (http.request + PII scan) is still verified",
        )

    overall = True
    try:
        if not _step1_infra_check():
            return 2

        result = _step2_drive_widget(api_base)
        if result is None:
            return 1
        trace_id, _channel_id = result
        _print("INFO", f"Tempo UI: {TEMPO_UI_URL.format(trace_id=trace_id)}")

        spans = _step3_query_tempo(trace_id)
        if spans is None:
            return 1
        overall &= _step4_assert_span_hierarchy(spans)
        overall &= _step5_assert_no_pii_in_spans(spans)
        overall &= _step6_assert_no_orphan_spans(spans)

        # Best-effort: fetch the LLM trace (the WS path's span tree)
        # and verify llm.chat + outbound httpx are present.
        llm_trace_id = _step3b_search_recent_llm_traces()
        if llm_trace_id:
            _print("INFO", f"LLM trace UI: {TEMPO_UI_URL.format(trace_id=llm_trace_id)}")
            overall &= _step4b_assert_llm_hierarchy(llm_trace_id)
            # PII scan: also apply to the LLM trace's spans.
            llm_spans = _step3_query_tempo(llm_trace_id)
            if llm_spans is not None:
                overall &= _step5_assert_no_pii_in_spans(llm_spans)
        else:
            _print(
                "WARN",
                "could not locate an LLM trace via Tempo search — "
                "the http.request hierarchy is verified but llm.chat "
                "hierarchy is best-effort. Verify manually in the Tempo UI.",
            )
    except requests.RequestException as exc:
        _print(
            "FAIL",
            f"HTTP layer failed: {type(exc).__name__}: {exc} — "
            "is the docker stack up?",
        )
        return 2
    except Exception as exc:
        _print("FAIL", f"unexpected crash: {type(exc).__name__}: {exc}")
        return 1
    finally:
        reset_engine()
        reset_sessionmaker()

    print("=" * 70)
    print(f"  RESULT: {'ALL E2E STEPS PASS' if overall else 'AT LEAST ONE STEP FAILED'}")
    print("=" * 70)
    return 0 if overall else 1


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="test_otel_e2e.py",
        description=(
            "End-to-end smoke for M6 OpenTelemetry tracing. Drives a "
            "real widget chat through the api container, captures the "
            "X-Trace-Id response header, queries Tempo for the trace, "
            "and asserts the span hierarchy + PII-safe attribute keys."
        ),
        epilog=(
            "Prereqs:\n"
            "  - docker compose stack up (api + otel-collector + tempo)\n"
            "  - DATABASE_URL + JWT_SECRET set (to seed a tenant)\n"
            "  - ANTHROPIC_API_KEY optional — without it the WS chat will\n"
            "    fall back to the AI-unavailable message but the trace\n"
            "    wiring is still verified.\n"
            "\n"
            "Examples:\n"
            "  python tests/e2e/scripts/test_otel_e2e.py --help\n"
            "  python tests/e2e/scripts/test_otel_e2e.py\n"
            "  python tests/e2e/scripts/test_otel_e2e.py \\\n"
            "      --api-base http://staging.lumen.internal:8000\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--api-base", default="http://localhost:8000",
        help="Base URL of the running api service (default: http://localhost:8000).",
    )
    return p


if __name__ == "__main__":
    args = _build_parser().parse_args()
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        _print("INFO", "interrupted by user")
        sys.exit(1)
