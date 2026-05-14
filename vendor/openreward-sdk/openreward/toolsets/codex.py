"""Codex session toolset.

Codex prefers a single shell tool over discrete file tools, so this toolset
exposes only ``bash``. The bash description is copied verbatim from the
firehorse codex descriptions (``firehorse/firehorse/mcp/codex_descriptions.py``)
which were extracted from upstream Codex (``codex-rs/tools/src/local_tool.rs``).
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from openreward.environments.environment import tool
from openreward.environments.toolset import Toolset
from openreward.environments.types import TextBlock, ToolOutput


class BashParams(BaseModel, extra="forbid"):
    command: str
    description: str = ""
    timeout: Optional[float] = 30.0


BASH_DESCRIPTION = (
    "Runs a shell command and returns its output. "
    "Always set workdir param; avoid cd unless absolutely necessary."
)


class CodexToolset(Toolset):
    """Session toolset exposing the Codex single-tool surface (``bash`` only).

    The toolset is bound to a session by passing it to ``env.session(...)``::

        from openreward.toolsets import CodexToolset

        with env.session(task=task, toolset="codex") as session:
            session.call_tool("bash", {"command": "ls"})

    Requires the bound environment to define ``self.sandbox``.
    """

    @classmethod
    def name(cls) -> str:
        return "codex"

    @tool
    async def bash(self, params: BashParams) -> ToolOutput:
        try:
            output, code = await self.sandbox.run(params.command.strip())
            return ToolOutput(
                blocks=[TextBlock(text=f"{output}\n\n(exit {code})")],
                metadata={"output": output, "exit_code": code},
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error executing command: {str(e)}")],
                finished=False,
            )


CodexToolset.bash.__doc__ = BASH_DESCRIPTION
