"""Base class for toolsets that provide reusable collections of tools."""

from abc import ABC
from typing import Any


class Toolset(ABC):
    """
    Optional base class for toolsets. Provides common utilities for
    sandbox-based toolsets.

    Toolsets are collections of related tools that can be easily reused
    across different environments. They follow the same @tool decorator
    pattern as Environment methods.

    Usage:
        from openreward.environments import Toolset, tool, ToolOutput

        class MyToolset(Toolset):
            @tool
            async def my_tool(self) -> ToolOutput:
                # Use self.sandbox to interact with sandbox
                output, code = await self.sandbox.run("ls")
                return ToolOutput(blocks=[TextBlock(text=output)])

        # In your environment:
        class MyEnv(Environment):
            toolsets = [MyToolset]

            def __init__(self, task_spec, secrets):
                super().__init__(task_spec, secrets)
                self.sandbox = ...  # MyToolset will access this automatically
    """

    def __init__(self, env: Any, sandbox_attr: str = "sandbox"):
        """
        Initialize toolset with environment dependencies.

        Args:
            env: The environment instance that owns this toolset.
            sandbox_attr: Name of sandbox attribute on env (default: "sandbox").
                         The toolset will look for this attribute and store it
                         as self.sandbox for easy access.

        Raises:
            ValueError: If env does not define ``sandbox_attr`` (or it is None).
        """
        self.env = env
        if not hasattr(env, sandbox_attr) or getattr(env, sandbox_attr) is None:
            raise ValueError(
                f"Toolset {type(self).__name__} requires `env.{sandbox_attr}` to be set "
                f"(typically assigned in the environment's __init__ or setup()) but the "
                f"environment does not define one or it is None."
            )
        self.sandbox = getattr(env, sandbox_attr)

    @classmethod
    def name(cls) -> str:
        """Stable identifier for this toolset, used for wire serialization."""
        return cls.__name__
