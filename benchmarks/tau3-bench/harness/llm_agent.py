from typing import Any, Generic, List, Optional, TypeVar

from pydantic import BaseModel

from harness.agent.base.llm_config import LLMConfigMixin
from harness.agent.base_agent import (
    HalfDuplexAgent,
    ValidAgentInputMessage,
    is_valid_agent_history_message,
)
from harness.data_model.message import (
    APICompatibleMessage,
    AssistantMessage,
    Message,
    MultiToolMessage,
    SystemMessage,
    ToolMessage,
)
from harness.environment.tool import Tool
from harness.utils.llm_utils import generate

MAX_VALIDATOR_BOUNCES = 3

AGENT_INSTRUCTION = """
You are a customer service agent that helps the user according to the <policy> provided below.
In each turn you can either:
- Send a message to the user.
- Make a tool call.
You cannot do both at the same time.

Try to be helpful and always follow the policy. Always make sure you generate valid JSON only.
""".strip()

SYSTEM_PROMPT = """
<instructions>
{agent_instruction}
</instructions>
<policy>
{domain_policy}
</policy>
""".strip()


class LLMAgentState(BaseModel):
    """The state of the agent."""

    system_messages: list[SystemMessage]
    messages: list[APICompatibleMessage]


LLMAgentStateType = TypeVar("LLMAgentStateType", bound="LLMAgentState")


class LLMAgent(
    LLMConfigMixin, HalfDuplexAgent[LLMAgentStateType], Generic[LLMAgentStateType]
):
    """A half-duplex text LLM agent."""

    def __init__(
        self,
        tools: List[Tool],
        domain_policy: str,
        llm: str,
        llm_args: Optional[dict] = None,
    ):
        """Initialize the LLM agent."""
        super().__init__(
            tools=tools,
            domain_policy=domain_policy,
            llm=llm,
            llm_args=llm_args,
        )

    @property
    def system_prompt(self) -> str:
        return SYSTEM_PROMPT.format(
            domain_policy=self.domain_policy,
            agent_instruction=AGENT_INSTRUCTION,
        )

    def get_init_state(
        self, message_history: Optional[list[Message]] = None
    ) -> LLMAgentStateType:
        """Get the initial state of the agent."""
        if message_history is None:
            message_history = []
        assert all(is_valid_agent_history_message(m) for m in message_history), (
            "Message history must contain only AssistantMessage, UserMessage, "
            "or ToolMessage to Agent."
        )
        return LLMAgentState(
            system_messages=[SystemMessage(role="system", content=self.system_prompt)],
            messages=message_history,
        )

    def generate_next_message(
        self, message: ValidAgentInputMessage, state: LLMAgentStateType
    ) -> tuple[AssistantMessage, LLMAgentStateType]:
        """Respond to a user or tool message."""
        assistant_message = self._generate_next_message(message, state)
        state.messages.append(assistant_message)
        return assistant_message, state

    def _generate_next_message(
        self, message: ValidAgentInputMessage, state: LLMAgentStateType
    ) -> AssistantMessage:
        """Generate the next assistant message, bouncing invalid write calls."""
        try:
            from harness.agent.write_validator import bounce_tool_calls
        except ImportError as error:  # never let validator wiring kill a trial
            print(f"WARNING: write validator unavailable ({error}); writes unvalidated")

            def bounce_tool_calls(tool_calls: Any, conversation: Any) -> dict:
                return {}

        if isinstance(message, MultiToolMessage):
            state.messages.extend(message.tool_messages)
        else:
            state.messages.append(message)
        bounces_left = MAX_VALIDATOR_BOUNCES
        while True:
            assistant_message = generate(
                model=self.llm,
                tools=self.tools,
                messages=state.system_messages + state.messages,
                call_name="agent_response",
                **self.llm_args,
            )
            tool_calls = assistant_message.tool_calls or []
            if bounces_left <= 0 or not tool_calls:
                return assistant_message
            conversation: list[dict[str, Any]] = [
                {
                    "role": m.role,
                    "content": getattr(m, "content", None),
                    "id": getattr(m, "id", None),
                    "error": getattr(m, "error", False),
                    "tool_calls": [
                        {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                        for tc in (getattr(m, "tool_calls", None) or [])
                    ],
                }
                for m in state.messages
            ]
            bounces = bounce_tool_calls(
                [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in tool_calls
                ],
                conversation,
            )
            if not bounces:
                return assistant_message
            bounces_left -= 1
            state.messages.append(assistant_message)
            state.messages.extend(
                ToolMessage(
                    id=tc.id,
                    role="tool",
                    content=bounces[tc.id],
                    requestor="assistant",
                    error=False,
                )
                for tc in tool_calls
            )


def create_llm_agent(tools, domain_policy, **kwargs):
    """Factory function for LLMAgent."""
    return LLMAgent(
        tools=tools,
        domain_policy=domain_policy,
        llm=kwargs.get("llm"),
        llm_args=kwargs.get("llm_args"),
    )
