"""SES inbound payload parser.

SES posts JSON to our webhook with spam/virus verdicts, commonHeaders,
and base64-decoded `content`. We only extract what we need for routing
and AI auto-reply; raw verdicts are logged but not enforced.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from email.utils import parseaddr


@dataclass(frozen=True)
class ParsedEmail:
    from_address: str  # bare email, no display name
    to_address: str
    subject: str
    body_text: str
    message_id: str  # angle-bracketed, e.g. <msg-001@example.com>
    in_reply_to: str | None
    references: list[str]
    thread_id: str  # In-Reply-To if set, else References[0] if set, else self.message_id

    def __post_init__(self) -> None:
        # Validate bare-email form (defensive: SES should guarantee this).
        # message_id / thread_id are angle-bracketed (RFC 2822 Message-ID form)
        # so they intentionally include '<' / '>' and are excluded here.
        for fld in ("from_address", "to_address"):
            v = getattr(self, fld)
            if not v or "<" in v or ">" in v:
                raise ValueError(f"{fld} must be a bare email without angle brackets: {v!r}")


def _strip_brackets(addr: str) -> str:
    """Strip angle brackets from a Message-ID style header value."""
    return addr.strip().lstrip("<").rstrip(">")


def _bare_email(addr_header: str) -> str:
    """Extract the bare email from a 'Display Name <addr@host>' style header."""
    _name, addr = parseaddr(addr_header)
    return addr.lower() if addr else ""


def parse_ses_inbound(payload: bytes) -> ParsedEmail | None:
    """Parse SES inbound webhook payload. Returns None on malformed input.

    Failure path: returns None rather than raising — SES retries on 5xx,
    so we want a deterministic 200 for malformed payloads (logged + dropped).
    """
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    headers = data.get("commonHeaders") or {}
    if not isinstance(headers, dict):
        return None

    from_list = headers.get("from") or []
    to_list = headers.get("to") or []
    subject = headers.get("subject") or ""
    raw_message_id = headers.get("messageId") or ""

    if not (isinstance(from_list, list) and from_list and isinstance(to_list, list) and to_list):
        return None
    if not raw_message_id:
        return None

    message_id = f"<{_strip_brackets(raw_message_id)}>"
    from_addr = _bare_email(from_list[0])
    to_addr = _bare_email(to_list[0])
    if not from_addr or not to_addr:
        return None

    in_reply_to_raw = headers.get("inReplyTo")
    in_reply_to = f"<{_strip_brackets(in_reply_to_raw)}>" if in_reply_to_raw else None

    references_raw = headers.get("references") or []
    if isinstance(references_raw, str):
        references_raw = [references_raw]
    references = [f"<{_strip_brackets(r)}>" for r in references_raw if r]

    # Thread routing: In-Reply-To > References[0] > self message_id
    if in_reply_to:
        thread_id = in_reply_to
    elif references:
        thread_id = references[0]
    else:
        thread_id = message_id

    content_raw = data.get("content", "")
    body_text = content_raw
    # Defensive: SES occasionally returns content as a dict (nested MIME parts
    # or wrapper objects) instead of a string. Slicing a dict would raise
    # TypeError. Per parser contract, return None for malformed payloads.
    if not isinstance(body_text, str):
        if isinstance(body_text, dict):
            body_text = body_text.get("data", "") or ""
            if not isinstance(body_text, str):
                return None
        else:
            return None
    if isinstance(body_text, str) and body_text.startswith("{"):
        # SES sometimes wraps MIME parts in a nested JSON
        try:
            nested = json.loads(body_text)
            if isinstance(nested, dict):
                inner = nested.get("data", body_text)
                if isinstance(inner, str):
                    body_text = inner
        except json.JSONDecodeError:
            pass

    # Truncate body to 100KB (LLM context safety)
    if len(body_text) > 100_000:
        body_text = body_text[:100_000]

    try:
        return ParsedEmail(
            from_address=from_addr,
            to_address=to_addr,
            subject=subject,
            body_text=body_text,
            message_id=message_id,
            in_reply_to=in_reply_to,
            references=references,
            thread_id=thread_id,
        )
    except ValueError:
        return None