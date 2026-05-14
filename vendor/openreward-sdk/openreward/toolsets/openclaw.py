"""OpenClaw session toolset.

Provides the six built-in coding tools OpenClaw exposes
(``exec``, ``process``, ``read``, ``write``, ``edit``, ``apply_patch``).
Tool names, parameter schemas, and descriptions match OpenClaw's
upstream definitions.
"""
from __future__ import annotations

import base64
import os
from typing import Any, List, Optional

from pydantic import BaseModel

from openreward.environments.environment import tool
from openreward.environments.toolset import Toolset
from openreward.environments.types import TextBlock, ToolOutput


async def _download_text(sandbox: Any, path: str) -> str:
    data = await sandbox.download(path)
    return data.decode("utf-8")


async def _upload_text(
    sandbox: Any,
    path: str,
    content: str,
    ensure_trailing_newline: bool = True,
) -> None:
    if ensure_trailing_newline and not content.endswith("\n"):
        content = content + "\n"
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    await sandbox.check_run(f"echo '{encoded}' | base64 -d > {path}")


# ── Pydantic parameter models ──

class ExecParams(BaseModel, extra="forbid"):
    command: str
    timeout: Optional[float] = 1800.0


class ReadParams(BaseModel, extra="forbid"):
    path: str
    offset: Optional[int] = None
    limit: Optional[int] = None


class WriteParams(BaseModel, extra="forbid"):
    path: str
    content: str


class EditItem(BaseModel, extra="forbid"):
    oldText: str
    newText: str


class EditParams(BaseModel, extra="forbid"):
    path: str
    edits: List[EditItem]


class ApplyPatchParams(BaseModel, extra="forbid"):
    input: str


class ProcessParams(BaseModel, extra="forbid"):
    action: str
    sessionId: Optional[str] = None
    data: Optional[str] = None
    eof: Optional[bool] = None
    offset: Optional[int] = None
    limit: Optional[int] = None


# ── Tool descriptions (matching OpenClaw upstream) ──

EXEC_DESCRIPTION = """\
Execute a shell command and return its output and exit code. \
Set timeout in seconds (default 1800). \
Use for builds, installs, git, processes, scripts, network, package managers, \
and anything that needs a shell."""

READ_DESCRIPTION = """\
Read the contents of a file at the given path. \
Supports optional offset (1-indexed line number to start from) and limit \
(maximum number of lines to read) for pagination of large files."""

WRITE_DESCRIPTION = """\
Write content to a file, creating it if it doesn't exist or overwriting if it does. \
Creates parent directories automatically. \
Use the edit tool for targeted modifications to existing files."""

EDIT_DESCRIPTION = """\
Apply one or more targeted text replacements to a file. \
Each edit specifies an oldText to find and a newText to replace it with. \
oldText must be unique in the file for each edit."""

PROCESS_DESCRIPTION = """\
Manage background processes. Use exec with background=true to start a process, \
then use this tool to interact with it.

Actions:
- list: show running and finished background sessions
- poll: check for new output from a session (requires sessionId)
- log: read session output with optional offset/limit pagination (requires sessionId)
- write: send data to a session's stdin (requires sessionId and data)
- kill: terminate a background session (requires sessionId)
- remove: kill if running, clear if finished (requires sessionId)"""

APPLY_PATCH_DESCRIPTION = """\
Apply file modifications using a structured patch format, designed for multiple file \
or multi-hunk edits where individual edit calls would be fragile.

The input must include '*** Begin Patch' and '*** End Patch' markers. Supported \
operations: '*** Add File:', '*** Update File:' (with optional '*** Move to:'), \
'*** Delete File:', and '*** End of File' for EOF-only insertions."""


# ── Toolset ──

class OpenClawToolset(Toolset):
    """Session toolset exposing the OpenClaw six-tool coding surface.

    The toolset is bound to a session by passing it to ``env.session(...)``::

        from openreward.toolsets import OpenClawToolset

        with env.session(task=task, toolset="openclaw") as session:
            session.call_tool("exec", {"command": "ls"})

    Requires the bound environment to define ``self.sandbox``.
    """

    @classmethod
    def name(cls) -> str:
        return "openclaw"

    @tool
    async def exec(self, params: ExecParams) -> ToolOutput:
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

    @tool
    async def process(self, params: ProcessParams) -> ToolOutput:
        try:
            action = params.action
            sid = params.sessionId or ""

            if action == "list":
                output, code = await self.sandbox.run("ps aux --no-headers 2>/dev/null || ps aux")
                return ToolOutput(
                    blocks=[TextBlock(text=output if output.strip() else "No background processes.")],
                    metadata={"output": output, "exit_code": code},
                    reward=0.0,
                    finished=False,
                )

            if not sid:
                return ToolOutput(
                    metadata={"error": "sessionId is required for this action"},
                    blocks=[TextBlock(text=f"Error: sessionId is required for action '{action}'.")],
                    finished=False,
                )

            if action in ("poll", "log"):
                tail_n = params.limit or 200
                cmd = f"cat /tmp/_oc_proc_{sid}.log 2>/dev/null || echo 'No output available for session {sid}'"
                if params.offset is not None:
                    cmd = f"tail -n +{params.offset} /tmp/_oc_proc_{sid}.log 2>/dev/null | head -n {tail_n}"
                elif params.limit:
                    cmd = f"tail -n {tail_n} /tmp/_oc_proc_{sid}.log 2>/dev/null"
                output, code = await self.sandbox.run(cmd)
                return ToolOutput(
                    blocks=[TextBlock(text=output)],
                    metadata={"output": output, "exit_code": code},
                    reward=0.0,
                    finished=False,
                )

            if action == "write":
                data = params.data or ""
                output, code = await self.sandbox.run(
                    f"echo '{data}' >> /tmp/_oc_proc_{sid}.stdin 2>/dev/null"
                )
                return ToolOutput(
                    blocks=[TextBlock(text=f"Sent data to session {sid}")],
                    metadata={"output": output, "exit_code": code},
                    reward=0.0,
                    finished=False,
                )

            if action in ("kill", "remove"):
                output, code = await self.sandbox.run(
                    f"kill $(cat /tmp/_oc_proc_{sid}.pid 2>/dev/null) 2>/dev/null; "
                    f"rm -f /tmp/_oc_proc_{sid}.pid /tmp/_oc_proc_{sid}.log /tmp/_oc_proc_{sid}.stdin"
                )
                return ToolOutput(
                    blocks=[TextBlock(text=f"Session {sid} terminated and cleaned up.")],
                    metadata={"output": output, "exit_code": code},
                    reward=0.0,
                    finished=False,
                )

            return ToolOutput(
                metadata={"error": f"Unknown action: {action}"},
                blocks=[TextBlock(text=f"Error: unknown process action '{action}'. "
                        "Use list, poll, log, write, kill, or remove.")],
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error managing process: {str(e)}")],
                finished=False,
            )

    @tool
    async def read(self, params: ReadParams) -> ToolOutput:
        try:
            content = await _download_text(self.sandbox, params.path)
            lines = content.splitlines()

            if params.offset is not None or params.limit is not None:
                start = (params.offset or 1) - 1
                if params.limit is not None:
                    lines = lines[start:start + params.limit]
                else:
                    lines = lines[start:]

            output = "\n".join(lines)
            return ToolOutput(
                metadata={"output": output, "exit_code": 0},
                blocks=[TextBlock(text=output)],
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error reading file: {str(e)}")],
                finished=False,
            )

    @tool
    async def write(self, params: WriteParams) -> ToolOutput:
        try:
            dir_name = os.path.dirname(params.path)
            if dir_name:
                await self.sandbox.run(f"mkdir -p {dir_name}")
            await _upload_text(
                self.sandbox,
                params.path,
                params.content,
                ensure_trailing_newline=True,
            )
            return ToolOutput(
                metadata={"output": "", "exit_code": 0},
                blocks=[TextBlock(text=f"Successfully wrote to {params.path}")],
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error writing file: {str(e)}")],
                finished=False,
            )

    @tool
    async def edit(self, params: EditParams) -> ToolOutput:
        try:
            content = await _download_text(self.sandbox, params.path)

            for item in params.edits:
                count = content.count(item.oldText)
                if count == 0:
                    return ToolOutput(
                        metadata={"error": f"oldText not found in {params.path}"},
                        blocks=[TextBlock(text=f"Error: oldText not found in {params.path}: {item.oldText[:100]}")],
                        finished=False,
                    )
                if count > 1:
                    return ToolOutput(
                        metadata={"error": f"oldText appears {count} times; must be unique"},
                        blocks=[TextBlock(text=f"Error: oldText appears {count} times in {params.path}. Must be unique.")],
                        finished=False,
                    )
                content = content.replace(item.oldText, item.newText, 1)

            await _upload_text(self.sandbox, params.path, content, ensure_trailing_newline=True)

            return ToolOutput(
                metadata={"output": "", "exit_code": 0},
                blocks=[TextBlock(text=f"Successfully edited {params.path}")],
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error editing file: {str(e)}")],
                finished=False,
            )

    @tool
    async def apply_patch(self, params: ApplyPatchParams) -> ToolOutput:
        try:
            patch_tmp = "/tmp/_openclaw_patch.diff"
            await _upload_text(self.sandbox, patch_tmp, params.input, ensure_trailing_newline=True)

            output, code = await self.sandbox.run(f"patch -p1 < {patch_tmp}")
            if code != 0:
                await self.sandbox.run(f"rm -f {patch_tmp}")
                return ToolOutput(
                    metadata={"error": output, "exit_code": code},
                    blocks=[TextBlock(text=f"apply_patch failed (exit {code}):\n{output}")],
                    finished=False,
                )

            await self.sandbox.run(f"rm -f {patch_tmp}")
            return ToolOutput(
                metadata={"output": output, "exit_code": 0},
                blocks=[TextBlock(text=f"Successfully applied patch:\n{output}")],
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error applying patch: {str(e)}")],
                finished=False,
            )


OpenClawToolset.exec.__doc__ = EXEC_DESCRIPTION
OpenClawToolset.process.__doc__ = PROCESS_DESCRIPTION
OpenClawToolset.read.__doc__ = READ_DESCRIPTION
OpenClawToolset.write.__doc__ = WRITE_DESCRIPTION
OpenClawToolset.edit.__doc__ = EDIT_DESCRIPTION
OpenClawToolset.apply_patch.__doc__ = APPLY_PATCH_DESCRIPTION
