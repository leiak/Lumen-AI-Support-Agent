"""Pydantic schemas for the per-tenant assignment policy.

Stored on ``tenants.settings['assignment_policy']`` as a sub-dict;
read via ``TenantRepository.get_assignment_policy`` and written
via ``TenantRepository.update_settings`` after a read-merge-write
cycle in the PATCH handler.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Recognised strategy values. ``manual`` is the explicit opt-out
# sentinel — admin has set the policy but doesn't want auto-assign
# (lets agents claim as before). ``least_busy`` is the only auto
# strategy today; new strategies get a new enum value + a new method
# on ``AssignmentPolicyService``.
AssignmentStrategy = Literal["manual", "least_busy"]


class AssignmentPolicyRead(BaseModel):
    """Read response. Materialised even when the policy is unset
    (defaults to ``enabled=False, strategy='manual'``) so the SPA
    can render the form without a special-cased null branch."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    strategy: AssignmentStrategy


class AssignmentPolicyUpdate(BaseModel):
    """PATCH payload. All fields optional; missing fields keep
    the current value. Strategy must be one of the recognised
    enum values — Pydantic rejects unknown strategies with 422
    before the handler ever runs."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = Field(
        default=None,
        description="When false (or unset on first save), the resolver is a no-op.",
    )
    strategy: AssignmentStrategy | None = Field(
        default=None,
        description="Strategy enum. 'manual' = explicit opt-out; 'least_busy' = auto.",
    )
