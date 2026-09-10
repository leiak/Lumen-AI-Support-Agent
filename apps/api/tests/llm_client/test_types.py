"""Tests for LLM client types and provider abstraction."""
import pytest
from pydantic import ValidationError

from llm_client.exceptions import (
    InvalidRequest,
    LLMError,
    OutputInvalid,
    ProviderUnavailable,
    RateLimited,
)
from llm_client.providers.base import BaseProvider
from llm_client.types import ChatMessage, ChatRequest, ChatResponse, MessageRole


def test_chat_request_validation() -> None:
    req = ChatRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[ChatMessage(role=MessageRole.USER, content="Hello")],
        temperature=0.0,
        max_tokens=100,
    )
    assert req.messages[0].content == "Hello"
    assert req.temperature == 0.0


def test_chat_response_total_tokens_property() -> None:
    resp = ChatResponse(
        content="Hi there",
        model="claude-3-5-sonnet-20241022",
        prompt_tokens=10,
        completion_tokens=5,
        finish_reason="stop",
    )
    assert resp.total_tokens == 15


def test_message_role_str_enum_values() -> None:
    assert MessageRole.SYSTEM == "system"
    assert MessageRole.USER == "user"
    assert MessageRole.ASSISTANT == "assistant"
    assert MessageRole.TOOL == "tool"


def test_chat_request_empty_messages_rejected() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(model="x", messages=[])


def test_chat_request_temperature_out_of_range() -> None:
    with pytest.raises(ValidationError):
        ChatRequest(
            model="x",
            messages=[ChatMessage(role=MessageRole.USER, content="hi")],
            temperature=3.0,  # max 2.0
        )


def test_exception_hierarchy() -> None:
    assert issubclass(ProviderUnavailable, LLMError)
    assert issubclass(RateLimited, LLMError)
    assert issubclass(InvalidRequest, LLMError)
    assert issubclass(OutputInvalid, LLMError)


def test_base_provider_is_abstract() -> None:
    """BaseProvider cannot be instantiated directly; subclasses must implement chat/stream."""
    with pytest.raises(TypeError):
        BaseProvider()  # type: ignore[abstract]
