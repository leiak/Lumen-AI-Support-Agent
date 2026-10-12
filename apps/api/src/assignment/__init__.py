"""Per-tenant conversation assignment policy engine.

The strategy layer is intentionally tiny. Adding a new strategy
(round-robin, skill-based, business-hours) is one new enum value +
one new method on :class:`AssignmentPolicyService` + the matching
handler in the admin API.
"""
