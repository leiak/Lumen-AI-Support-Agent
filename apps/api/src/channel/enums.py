"""Channel-domain enums (channel type, status)."""
from enum import StrEnum


class ChannelType(StrEnum):
    """Messaging platform this channel connects to."""

    FEISHU = "feishu"
    WEB = "web"
    EMAIL = "email"


class ChannelStatus(StrEnum):
    """Whether the channel is currently active."""

    ACTIVE = "active"
    DISABLED = "disabled"