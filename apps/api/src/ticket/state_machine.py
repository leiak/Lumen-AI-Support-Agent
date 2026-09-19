"""Ticket lifecycle state machine.

Seven statuses (see ``ticket.enums.TicketStatus``). Transitions are
governed by the ``_TRANSITIONS`` map below — anything not listed is
illegal and raises :class:`InvalidTransition`.

Closure properties:
  * ``CLOSED`` and ``CANCELLED`` are terminal: no outbound transitions.
  * ``RESOLVED`` can reopen back to ``IN_PROGRESS`` (agent reopens after
    a customer reports the issue isn't actually fixed).
"""
from ticket.enums import TicketStatus


class InvalidTransition(Exception):
    """Raised when a status transition violates the state machine."""


_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.NEW: {TicketStatus.TRIAGED, TicketStatus.CANCELLED},
    TicketStatus.TRIAGED: {TicketStatus.IN_PROGRESS, TicketStatus.CANCELLED},
    TicketStatus.IN_PROGRESS: {
        TicketStatus.WAITING_CUSTOMER,
        TicketStatus.RESOLVED,
        TicketStatus.CANCELLED,
    },
    TicketStatus.WAITING_CUSTOMER: {
        TicketStatus.IN_PROGRESS,
        TicketStatus.RESOLVED,
        TicketStatus.CANCELLED,
    },
    TicketStatus.RESOLVED: {TicketStatus.CLOSED, TicketStatus.IN_PROGRESS},  # reopen
    TicketStatus.CLOSED: set(),
    TicketStatus.CANCELLED: set(),
}


def transition(current: TicketStatus, target: TicketStatus) -> TicketStatus:
    """Validate the ``current -> target`` transition and return ``target``.

    Raises :class:`InvalidTransition` if the transition is not in the
    state machine. Pure function — no DB / I/O. The caller is
    responsible for any persistence / side effects.
    """
    if target not in _TRANSITIONS[current]:
        raise InvalidTransition(
            f"Cannot transition from {current.value} to {target.value}"
        )
    return target