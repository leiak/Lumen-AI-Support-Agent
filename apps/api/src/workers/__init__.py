"""Arq worker entry points.

This package is the on-disk location of the ``WorkerSettings`` class
that ``arq`` reads when it starts the background worker process. The
M1 ``pyproject.toml`` declares the ``arq`` dependency (Task 7 + Task
8 wiring) but the actual ``WorkerSettings`` lives in
:mod:`qa.worker` — the QA worker is the only registered function +
cron for the M2.A rollout.

The Docker / Makefile command (``arq workers.WorkerSettings``) is the
single point of truth for what the worker process boots.
"""
from __future__ import annotations

from qa.worker import WorkerSettings

__all__ = ["WorkerSettings"]