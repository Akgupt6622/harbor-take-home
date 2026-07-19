import json
from typing import Literal, Optional

from pydantic import BaseModel, Field

from harness.utils.utils import get_now

SystemRole = Literal["system"]
UserRole = Literal["user"]
AssistantRole = Literal["assistant"]
ToolRole = Literal["tool"]
ToolRequestor = UserRole | AssistantRole


class SystemMessage(BaseModel):
    """A system message."""

    role: SystemRole = Field(description="The role of the message sender.")
    content: Optional[str] = Field(
        description="The content of the message.", default=None
    )
    turn_idx: Optional[int] = Field(
        description="The index of the turn in the conversation.", default=None
    )
    timestamp: Optional[str] = Field(
        description="The timestamp of the message.", default_factory=get_now
    )

    def __str__(self) -> str:
        lines = ["SystemMessage"]
        if self.turn_idx is not None:
            lines.append(f"turn_idx: {self.turn_idx}")
        if self.timestamp is not None:
            lines.append(f"timestamp: {self.timestamp}")
        if self.content is not None:
            lines.append(f"content: {self.content}")
        return "\n".join(lines)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SystemMessage):
            return False
        return self.role == other.role and self.content == other.content


class ToolCall(BaseModel):
    """A tool call."""

    id: str = Field(default="", description="The unique identifier for the tool call.")
    name: str = Field(description="The name of the tool.")
    arguments: dict = Field(description="The arguments of the tool.")
    requestor: ToolRequestor = Field(
        "assistant",
        description="The requestor of the tool call.",
    )

    def __str__(self) -> str:
        lines = [f"ToolCall (from {self.requestor})"]
        if self.id:
            lines.append(f"id: {self.id}")
        lines.append(f"name: {self.name}")
        lines.append(f"arguments:\n{json.dumps(self.arguments, indent=2)}")
        return "\n".join(lines)

    @classmethod
    def from_string(cls, string: str) -> "ToolCall":
        """Parse the string representation produced by ``__str__``."""
        lines = string.strip().split("\n")
        first_line = lines[0]
        if "from user" in first_line:
            requestor = "user"
        else:
            requestor = "assistant"

        tool_id = ""
        name = ""
        arguments = {}
        i = 1
        while i < len(lines):
            line = lines[i]
            if line.startswith("id: "):
                tool_id = line[4:].strip()
            elif line.startswith("name: "):
                name = line[6:].strip()
            elif line.startswith("arguments:"):
                arguments = json.loads("\n".join(lines[i + 1 :]))
                break
            i += 1

        return cls(
            id=tool_id,
            name=name,
            arguments=arguments,
            requestor=requestor,
        )


class ParticipantMessageBase(BaseModel):
    """Base text message from a conversation participant."""

    role: str = Field(description="The role of the message sender.")
    content: Optional[str] = Field(
        description="The content of the message.",
        default=None,
    )
    tool_calls: Optional[list[ToolCall]] = Field(
        description="The tool calls made in the message.", default=None
    )
    turn_idx: Optional[int] = None
    timestamp: Optional[str] = Field(default_factory=get_now)
    cost: Optional[float] = None
    usage: Optional[dict] = None
    raw_data: Optional[dict] = None
    generation_time_seconds: Optional[float] = Field(
        description="Wall clock time in seconds for LLM generation.",
        default=None,
    )

    def validate(self) -> None:
        """Ensure the message has either text content or tool calls."""
        if not (self.has_content() or self.is_tool_call()):
            raise ValueError(
                f"{self.__class__.__name__} must have either content or tool_calls. "
                f"Got {self}"
            )

    def has_content(self) -> bool:
        """Check if the message has non-empty text content."""
        return self.content is not None and bool(self.content.strip())

    def has_text_content(self) -> bool:
        """Check if the message has non-empty text content."""
        return self.has_content()

    def is_tool_call(self) -> bool:
        """Check if the message contains tool calls."""
        return self.tool_calls is not None

    def __str__(self) -> str:
        lines = [f"{self.role.capitalize()}Message"]
        if self.has_text_content():
            lines.append(f"content: {self.content}")
        if self.is_tool_call():
            lines.append("ToolCalls:")
            lines.extend([str(tc) for tc in self.tool_calls or []])
        return "\n".join(lines)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ParticipantMessageBase):
            return False
        return (
            self.role == other.role
            and self.content == other.content
            and self.tool_calls == other.tool_calls
        )


class AssistantMessage(ParticipantMessageBase):
    """A text message from the assistant."""

    role: AssistantRole = Field(description="The role of the message sender.")

    @classmethod
    def text(
        cls,
        content: str,
        *,
        tool_calls: Optional[list[ToolCall]] = None,
        cost: Optional[float] = None,
        usage: Optional[dict] = None,
        raw_data: Optional[dict] = None,
        generation_time_seconds: Optional[float] = None,
    ) -> "AssistantMessage":
        """Create a text-only assistant message."""
        return cls(
            role="assistant",
            content=content,
            tool_calls=tool_calls,
            cost=cost,
            usage=usage,
            raw_data=raw_data,
            generation_time_seconds=generation_time_seconds,
        )


class UserMessage(ParticipantMessageBase):
    """A text message from the user."""

    role: UserRole = Field(description="The role of the message sender.")

    @classmethod
    def text(
        cls,
        content: str,
        *,
        tool_calls: Optional[list[ToolCall]] = None,
        cost: Optional[float] = None,
        usage: Optional[dict] = None,
        raw_data: Optional[dict] = None,
        generation_time_seconds: Optional[float] = None,
    ) -> "UserMessage":
        """Create a text-only user message."""
        return cls(
            role="user",
            content=content,
            tool_calls=tool_calls,
            cost=cost,
            usage=usage,
            raw_data=raw_data,
            generation_time_seconds=generation_time_seconds,
        )


class ToolMessage(BaseModel):
    """A message from a tool."""

    id: str = Field(description="The unique identifier for the tool call.")
    role: ToolRole = Field(description="The role of the message sender.")
    content: Optional[str] = Field(description="The output of the tool.", default=None)
    requestor: ToolRequestor = Field(
        "assistant",
        description="The requestor of the tool call.",
    )
    error: bool = Field(description="Whether the tool call failed.", default=False)
    turn_idx: Optional[int] = Field(
        description="The index of the turn in the conversation.", default=None
    )
    timestamp: Optional[str] = Field(
        description="The timestamp of the message.", default_factory=get_now
    )

    def __str__(self) -> str:
        lines = [f"ToolMessage (responding to {self.requestor})"]
        if self.turn_idx is not None:
            lines.append(f"turn_idx: {self.turn_idx}")
        if self.timestamp is not None:
            lines.append(f"timestamp: {self.timestamp}")
        if self.content is not None:
            lines.append(f"content: {self.content}")
        if self.error:
            lines.append("Error")
        return "\n".join(lines)

    def __eq__(self, other: object) -> bool:
        if type(other) is not type(self):
            return False
        return (
            self.id == other.id
            and self.role == other.role
            and self.content == other.content
            and self.requestor == other.requestor
            and self.error == other.error
        )


class MultiToolMessage(BaseModel):
    """Encapsulates multiple tool messages."""

    role: ToolRole = Field(description="The role of the message sender.")
    tool_messages: list[ToolMessage] = Field(description="The tool messages.")


APICompatibleMessage = SystemMessage | AssistantMessage | UserMessage | ToolMessage
Message = (
    SystemMessage | AssistantMessage | UserMessage | ToolMessage | MultiToolMessage
)
EnvironmentMessage = ToolMessage | MultiToolMessage
ValidInputMessage = UserMessage | AssistantMessage | EnvironmentMessage
