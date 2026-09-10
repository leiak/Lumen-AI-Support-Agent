"""Enums for conversations and messages."""
from enum import StrEnum


class ConversationStatus(StrEnum):
    OPEN = "open"
    PENDING = "pending"  # waiting for human agent
    CLOSED = "closed"


class MessageRole(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    AI = "ai"
    SYSTEM = "system"
    TOOL = "tool"
