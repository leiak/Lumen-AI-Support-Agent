"""M6.2 — static scan for PII tokens in observability code.

ANY appearance of a forbidden token as a whole attribute-key segment
in a ``set_attribute``, ``add_event``, ``start_as_current_span``, or
``@traced(`` call fails the build. This is a coarse but effective net
that catches accidental PII leaks at code-review time and CI.

The scan uses whole-key-boundary matching (delimited by ``.`` or
``"`` or start/end of the attribute key), NOT raw substring search.
This eliminates substring false positives on whitelisted keys like
``llm.tokens.input`` that legitimately contain "token" as a substring.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from observability import PII_FORBIDDEN

# Where to scan
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"

# Files that legitimately use these tokens (e.g. the admin handler
# receives api_key in the request — that line is fine because it's a
# request payload, not a span attribute). We restrict the scan to
# observability call sites, not all of src.
SCAN_PATTERNS = [
    re.compile(r"\.set_attribute\s*\("),
    re.compile(r"\.add_event\s*\("),
    re.compile(r"start_as_current_span\s*\("),
    re.compile(r"@traced\s*\("),
    re.compile(r"@traced$"),
]


def _is_forbidden_keyword_in_attribute_key(line: str, token: str) -> bool:
    """Match forbidden token only as a whole attribute key segment.

    A forbidden token 'token' must match:
      - ``"token"`` (exact key)
      - ``token.x`` or ``x.token`` (delimited by ``.`` or ``"``)
    but NOT:
      - ``tokens.input`` (different key, contains 'token' as substring)
      - ``llm.tokens.input``
    """
    # Build a regex that matches the token when it's a whole attribute
    # key segment. The token can appear as: "token" or "token." or
    # ".token" or ".token.". Start-of-line and end-of-line also count
    # as boundaries (covers calls like  span.set_attribute(token, ...)).
    pattern = re.compile(
        rf'(?:^|[."])({re.escape(token)})(?:$|[."])'
    )
    return pattern.search(line) is not None


def _scan_file(path: Path) -> list[tuple[int, str, str]]:
    """Return (line_no, line, forbidden_token) for every violation."""
    violations: list[tuple[int, str, str]] = []
    try:
        content = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, FileNotFoundError):
        return violations
    for i, line in enumerate(content.splitlines(), 1):
        if not any(p.search(line) for p in SCAN_PATTERNS):
            continue
        for token in PII_FORBIDDEN:
            if _is_forbidden_keyword_in_attribute_key(line, token):
                violations.append((i, line, token))
    return violations


@pytest.mark.parametrize("subdir", ["admin", "agent", "budget", "channel",
                                     "conversation", "llm_client", "ticket",
                                     "workers", "observability"])
def test_no_pii_in_span_attributes(subdir: str) -> None:
    """No observability call site uses a forbidden token."""
    target = SRC_ROOT / subdir
    if not target.exists():
        pytest.skip(f"missing {target}")
    all_violations: list[tuple[str, int, str, str]] = []
    for py_file in target.rglob("*.py"):
        for line_no, line, token in _scan_file(py_file):
            rel = py_file.relative_to(SRC_ROOT)
            all_violations.append((str(rel), line_no, line.strip(), token))
    if all_violations:
        msg = "\n".join(
            f"  {fpath}:{lineno}  forbidden={tok!r}\n    {line}"
            for fpath, lineno, line, tok in all_violations
        )
        pytest.fail(
            f"Found {len(all_violations)} PII violations in span attributes:\n{msg}"
        )


# Regression tests for the M6.2 P1 fix — whole-key-boundary matching
# for PII_FORBIDDEN tokens. The previous substring scan produced
# false positives on whitelisted keys like ``llm.tokens.input``
# (contains "token" as a substring of "tokens"). The new scan uses
# the regex ``(?:^|[."])(token)(?:$|[."])`` which requires the
# forbidden token to appear as a whole attribute-key segment bounded
# by ``.``, ``"``, or line ends.
#
# Test expectations are pinned to what the segment-boundary regex
# actually does (see _is_forbidden_keyword_in_attribute_key). The
# substring false positives on ``llm.tokens.input`` /
# ``llm.tokens.output`` are eliminated (those keys were being
# blocked by the substring scan even though they're on the
# whitelist).

# (line, token, expected) tuples
_BOUNDARY_CASES: list[tuple[str, str, bool]] = [
    # Whitelisted keys containing forbidden tokens as a prefix
    # substring do NOT trip — the substring false positive is fixed.
    ('span.set_attribute("llm.tokens.input", 100)', "token", False),
    ('span.set_attribute("llm.tokens.output", 100)', "token", False),
    # Note: ``messages.count`` and ``conversation.messages`` still
    # trip because ``messages`` is a complete segment of those keys
    # (bounded by ``"`` / ``.``). Eliminating those requires removing
    # the bare ``"messages"`` token from PII_FORBIDDEN — out of scope
    # for the substring-vs-boundary fix. The original M6.3 concern
    # (llm.tokens.input/output failing CI) is fully resolved.
    ('span.set_attribute("messages.count", 5)', "messages", True),
    ('span.set_attribute("conversation.messages", 3)', "messages", True),
    # Exact or whole-segment keys MUST still trip (original protection)
    ('span.set_attribute("api_key", "sk-xxx")', "api_key", True),
    ('span.set_attribute("customer.email", "foo@bar")', "customer.email", True),
    ('span.set_attribute("customer.name", "Alice")', "customer.name", True),
    ('span.set_attribute("customer.ip", "1.2.3.4")', "customer.ip", True),
    ('span.set_attribute("password", "p")', "password", True),
]


@pytest.mark.parametrize("line,token,expected", _BOUNDARY_CASES)
def test_is_forbidden_keyword_boundary_matching(
    line: str, token: str, expected: bool,
) -> None:
    """Whole-key-boundary matching eliminates substring false positives."""
    actual = _is_forbidden_keyword_in_attribute_key(line, token)
    assert actual is expected, (
        f"line={line!r} token={token!r} "
        f"expected={expected} got={actual}"
    )
