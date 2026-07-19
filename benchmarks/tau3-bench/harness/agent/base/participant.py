from abc import ABC, abstractmethod
from typing import Generic, Optional, TypeVar

from loguru import logger

from harness.data_model.message import Message

InputMessageType = TypeVar("InputMessageType", bound=Message)
OutputMessageType = TypeVar("OutputMessageType", bound=Message)
StateType = TypeVar("StateType")


class HalfDuplexParticipant(
    ABC, Generic[InputMessageType, OutputMessageType, StateType]
):
    """Protocol for turn-based text communication."""

    @abstractmethod
    def generate_next_message(
        self, message: InputMessageType, state: StateType
    ) -> tuple[OutputMessageType, StateType]:
        """Generate the next message from an input message and current state."""
        raise NotImplementedError

    @abstractmethod
    def get_init_state(
        self,
        message_history: Optional[list[Message]] = None,
    ) -> StateType:
        """Get the initial state of the participant."""
        raise NotImplementedError

    def stop(
        self,
        message: Optional[InputMessageType] = None,
        state: Optional[StateType] = None,
    ) -> None:
        """Stop the participant."""
        pass

    @classmethod
    def is_stop(cls, message: OutputMessageType) -> bool:
        """Return whether the message indicates that the participant stopped."""
        return False

    def set_seed(self, seed: int) -> None:
        """Set the participant seed if supported."""
        logger.warning(
            f"Setting seed for participant is not implemented for class "
            f"{self.__class__.__name__}"
        )
