"""Exceptions raised by the Feishu OpenAPI client."""


class FeishuAPIError(Exception):
    """Feishu OpenAPI returned a non-zero code or transport failure."""