"""Resilience suite conftest.

Resilience tests do their own fault-injection via ``monkeypatch`` /
``unittest.mock.patch`` — no shared fixtures needed. Singleton reset /
event-loop hygiene is owned by the parent ``apps/api/conftest.py``.
"""
from __future__ import annotations

__all__: list[str] = []
