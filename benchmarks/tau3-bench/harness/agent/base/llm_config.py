from copy import deepcopy
from typing import Optional

from loguru import logger


class LLMConfigMixin:
    """Shared LLM model configuration for the text agent."""

    llm: str
    llm_args: dict

    def __init__(
        self,
        *args,
        llm: str,
        llm_args: Optional[dict] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.llm = llm
        self.llm_args = deepcopy(llm_args) if llm_args is not None else {}

    def set_seed(self, seed: int):
        """Set the seed for the LLM."""
        if self.llm is None:
            raise ValueError("LLM is not set")
        cur_seed = self.llm_args.get("seed", None)
        if cur_seed is not None:
            logger.warning(f"Seed is already set to {cur_seed}, resetting it to {seed}")
        self.llm_args["seed"] = seed
