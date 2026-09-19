"""State machine unit tests for the Ticket lifecycle."""
import pytest

from ticket.enums import TicketStatus
from ticket.state_machine import _TRANSITIONS, InvalidTransition, transition


class TestTransitions:
    def test_new_to_triaged(self):
        assert transition(TicketStatus.NEW, TicketStatus.TRIAGED) == TicketStatus.TRIAGED

    def test_new_to_cancelled(self):
        assert transition(TicketStatus.NEW, TicketStatus.CANCELLED) == TicketStatus.CANCELLED

    def test_triaged_to_in_progress(self):
        assert transition(TicketStatus.TRIAGED, TicketStatus.IN_PROGRESS) == TicketStatus.IN_PROGRESS

    def test_in_progress_to_waiting_customer(self):
        assert transition(TicketStatus.IN_PROGRESS, TicketStatus.WAITING_CUSTOMER) == TicketStatus.WAITING_CUSTOMER

    def test_waiting_customer_back_to_in_progress(self):
        assert transition(TicketStatus.WAITING_CUSTOMER, TicketStatus.IN_PROGRESS) == TicketStatus.IN_PROGRESS

    def test_in_progress_to_resolved(self):
        assert transition(TicketStatus.IN_PROGRESS, TicketStatus.RESOLVED) == TicketStatus.RESOLVED

    def test_resolved_to_closed(self):
        assert transition(TicketStatus.RESOLVED, TicketStatus.CLOSED) == TicketStatus.CLOSED

    def test_invalid_new_to_closed(self):
        with pytest.raises(InvalidTransition):
            transition(TicketStatus.NEW, TicketStatus.CLOSED)

    def test_invalid_closed_to_anything(self):
        for s in TicketStatus:
            if s == TicketStatus.CLOSED:
                continue
            with pytest.raises(InvalidTransition):
                transition(TicketStatus.CLOSED, s)

    def test_all_states_have_transitions(self):
        assert len(_TRANSITIONS) == len(TicketStatus)