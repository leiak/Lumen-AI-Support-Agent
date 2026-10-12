"""M6.2 — static scan for PII tokens in observability code.

ANY appearance of a forbidden token in a ``set_attribute``,
``add_event``, ``start_as_current_span``, or ``@traced(`` call fails
the build. This is a coarse but effective net that catches accidental
PII leaks at code-review time and CI.
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
            if token in line:
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
