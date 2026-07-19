from enum import Enum
from typing import Any, Callable, Dict, Optional, TypeVar

from harness.environment.db import DB
from harness.environment.tool import Tool, as_tool
from harness.utils import get_dict_hash, update_pydantic_model_with_dict

TOOL_ATTR = "__tool__"
TOOL_TYPE_ATTR = "__tool_type__"
MUTATES_STATE_ATTR = "__mutates_state__"
DISCOVERABLE_ATTR = "__discoverable__"


T = TypeVar("T", bound=DB)


class ToolKitType(type):
    """Metaclass for ToolKit classes."""

    def __init__(cls, name, bases, attrs):
        func_tools = {}
        for name, method in attrs.items():
            if isinstance(method, property):
                method = method.fget
            if hasattr(method, TOOL_ATTR):
                func_tools[name] = method

        @property
        def _func_tools(self) -> Dict[str, Callable]:
            """Get the tools available in the ToolKit."""
            all_func_tools = func_tools.copy()
            try:
                all_func_tools.update(super(cls, self)._func_tools)
            except AttributeError:
                pass
            return all_func_tools

        cls._func_tools = _func_tools


class ToolType(str, Enum):
    """Conceptual classification of a tool."""

    READ = "read"
    WRITE = "write"
    THINK = "think"
    GENERIC = "generic"


def is_tool(
    tool_type: ToolType = ToolType.READ,
    mutates_state: Optional[bool] = None,
):
    """Decorator to mark a function as a tool.

    Args:
        tool_type: Conceptual classification (READ, WRITE, THINK, GENERIC).
        mutates_state: Accepted for compatibility with existing domain
            decorators. It is not used by the default text runtime.
    """
    if mutates_state is None:
        mutates_state = tool_type == ToolType.WRITE

    def decorator(func):
        setattr(func, TOOL_ATTR, True)
        setattr(func, TOOL_TYPE_ATTR, tool_type)
        setattr(func, MUTATES_STATE_ATTR, mutates_state)
        return func

    return decorator


def is_discoverable_tool(
    tool_type: ToolType = ToolType.READ,
    mutates_state: Optional[bool] = None,
):
    """Decorator to mark a function as a discoverable tool.

    Discoverable tools are tools that exist and can be called, but are not
    included in the agent's system prompt by default. The agent must discover
    these tools through the knowledge base and unlock them before calling.

    The docstring of the decorated function serves as the tool definition:
    - First paragraph: tool description
    - Args section: parameter definitions (parsed for name, type hint, and description)
    - The function name is the tool name

    Args:
        tool_type: Conceptual classification (READ, WRITE, THINK, GENERIC).
        mutates_state: Accepted for compatibility with existing domain
            decorators. It is not used by the default text runtime.
    """
    if mutates_state is None:
        mutates_state = tool_type == ToolType.WRITE

    def decorator(func):
        setattr(func, TOOL_ATTR, True)
        setattr(func, TOOL_TYPE_ATTR, tool_type)
        setattr(func, MUTATES_STATE_ATTR, mutates_state)
        setattr(func, DISCOVERABLE_ATTR, True)
        return func

    return decorator


class ToolKitBase(metaclass=ToolKitType):
    """Base class for ToolKit classes."""

    def __init__(self, db: Optional[T] = None):
        self.db: Optional[T] = db

    @property
    def tools(self) -> Dict[str, Callable]:
        """Get the tools available in the ToolKit."""
        return {name: getattr(self, name) for name in self._func_tools.keys()}

    def use_tool(self, tool_name: str, **kwargs) -> str:
        """Use a tool."""
        if tool_name not in self.tools:
            raise ValueError(f"Tool '{tool_name}' not found.")
        return self.tools[tool_name](**kwargs)

    def get_tools(self, include: Optional[list[str]] = None) -> Dict[str, Tool]:
        """Get the non-discoverable tools available in the ToolKit.

        Discoverable tools are excluded — they should not appear in the
        agent's system prompt. Use `get_discoverable_tools()` to access them.

        Args:
            include: If provided, only return tools whose names are in this list.
                If None, return all non-discoverable tools (no filtering).

        Returns:
            A dictionary of non-discoverable tools available in the ToolKit.
        """
        # NOTE: as_tool needs to get the function (self.foo), not the `foo(self, ...)`
        # Otherwise, the `self` will exists in the arguments.
        # Therefore, it needs to be called with getattr(self, name)
        tools = {
            name: as_tool(tool)
            for name, tool in self.tools.items()
            if not getattr(tool, DISCOVERABLE_ATTR, False)
        }
        if include is not None:
            allowed = set(include)
            unknown = allowed - set(tools.keys())
            if unknown:
                available = sorted(tools.keys())
                raise ValueError(
                    f"Tool(s) not found: {sorted(unknown)}. Available: {available}"
                )
            tools = {name: tool for name, tool in tools.items() if name in allowed}
        return tools

    def has_tool(self, tool_name: str) -> bool:
        """Check if a tool exists in the ToolKit."""
        return tool_name in self.tools

    def is_discoverable(self, tool_name: str) -> bool:
        """Check if a tool is a discoverable tool."""
        if tool_name not in self.tools:
            return False
        return getattr(self.tools[tool_name], DISCOVERABLE_ATTR, False)

    def get_discoverable_tools(self) -> Dict[str, Callable]:
        """Get all discoverable tool methods on this toolkit."""
        return {
            name: tool
            for name, tool in self.tools.items()
            if getattr(tool, DISCOVERABLE_ATTR, False)
        }

    def has_discoverable_tool(self, tool_name: str) -> bool:
        """Check if a discoverable tool exists."""
        return tool_name in self.get_discoverable_tools()

    def tool_type(self, tool_name: str) -> ToolType:
        """Get the type of a tool."""
        return getattr(self.tools[tool_name], TOOL_TYPE_ATTR)

    def update_db(self, update_data: Optional[dict[str, Any]] = None):
        """Update the database of the ToolKit."""
        if update_data is None:
            update_data = {}
        if self.db is None:
            raise ValueError("Database has not been initialized.")
        self.db = update_pydantic_model_with_dict(self.db, update_data)

    def get_db_hash(self) -> str:
        """Get the hash of the database."""
        return get_dict_hash(self.db.model_dump())
