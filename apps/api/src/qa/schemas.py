"""Pydantic schemas for the QA scoring API.

Stage 14 / Task 8 — the read-side schema for ``MessageQaScore`` rows so
admins / dashboards can list flagged turns. The score columns are
``float`` (not ``Decimal``) at every layer — see the rationale in
:mod:`qa.models` docstring.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class MessageQaScoreOut(BaseModel):
    """Read-only view of a QA score row.

    ``from_attributes=True`` lets us return ORM ``MessageQaScore``
    instances directly from FastAPI handlers without a manual
    conversion step. The four score fields are floats in [0, 1] —
    matches the Judge output range and the DB column type.
    """

    id: str
    message_id: str
    judge_model: str
    relevance_score: float
    safety_score: float
    faithfulness_score: float
    overall_score: float
    rationale: str | None
    flagged: bool
    created_at: datetime

    model_config = {"from_attributes": True}


__all__ = ["MessageQaScoreOut"]