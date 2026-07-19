from abc import ABC
from typing import Generic, Optional, TypeVar

from harness.agent.base.participant import HalfDuplexParticipant
from harness.data_model.message import (
    AssistantMessage,
    Message,
    MultiToolMessage,
    ToolMessage,
    UserMessage,
)
from harness.environment.tool import Tool

AgentState = TypeVar("AgentState")
ValidAgentInputMessage = UserMessage | ToolMessage | MultiToolMessage


class AgentError(Exception):
    """Generic exception for agent errors."""


def is_valid_agent_history_message(message: Message) -> bool:
    """Check if the message is valid text-agent history."""
    return (
        isinstance(message, AssistantMessage)
        or (isinstance(message, UserMessage) and not message.is_tool_call())
        or (isinstance(message, ToolMessage) and message.requestor == "assistant")
    )


class HalfDuplexAgent(
    HalfDuplexParticipant[ValidAgentInputMessage, AssistantMessage, AgentState],
    ABC,
    Generic[AgentState],
):
    """Base class for turn-based text agents."""

    def __init__(self, tools: list[Tool], domain_policy: str):
        super().__init__()
        self.tools = tools
        self.domain_policy = domain_policy

    def stop(
        self,
        message: Optional[ValidAgentInputMessage] = None,
        state: Optional[AgentState] = None,
    ) -> None:
        """Stop the agent."""
        pass


def validate_message_format(message: AssistantMessage) -> tuple[bool, str | None]:
    """Validate the default text agent response format."""
    has_content = message.has_text_content()
    is_tool_call = message.is_tool_call()
    if not has_content and not is_tool_call:
        return (
            False,
            "Each assistant message must contain either text content or tool calls.",
        )
    if has_content and is_tool_call:
        return (
            False,
            "Each assistant message must contain either text content or tool calls, not both.",
        )
    return True, None
