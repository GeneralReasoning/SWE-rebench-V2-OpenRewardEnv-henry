"""Hermes Agent session toolset.

Provides the five built-in coding tools Hermes Agent exposes
(``terminal``, ``read_file``, ``write_file``, ``search_files``, ``patch``).
Tool names, parameter schemas, and descriptions match Hermes Agent's
upstream registry definitions (``nousresearch/hermes-agent``).
"""
from __future__ import annotations

import base64
import os
from typing import Any, Optional

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

class TerminalParams(BaseModel, extra="forbid"):
    command: str
    timeout: Optional[int] = 180


class ReadFileParams(BaseModel, extra="forbid"):
    path: str
    offset: int = 1
    limit: int = 500


class WriteFileParams(BaseModel, extra="forbid"):
    path: str
    content: str


class SearchFilesParams(BaseModel, extra="forbid"):
    pattern: str
    target: str = "content"
    path: str = "."
    file_glob: Optional[str] = None
    limit: int = 50
    offset: int = 0
    output_mode: str = "content"
    context: int = 0


class PatchParams(BaseModel, extra="forbid"):
    mode: str = "replace"
    path: Optional[str] = None
    old_string: Optional[str] = None
    new_string: Optional[str] = None
    replace_all: bool = False
    patch: Optional[str] = None


# ── Tool descriptions (matching Hermes upstream registry) ──

TERMINAL_DESCRIPTION = """\
Execute shell commands on a Linux environment. Filesystem usually persists between calls.

Do NOT use cat/head/tail to read files — use read_file instead.
Do NOT use grep/rg/find to search — use search_files instead.
Do NOT use ls to list directories — use search_files(target='files') instead.
Do NOT use sed/awk to edit files — use patch instead.
Do NOT use echo/cat heredoc to create files — use write_file instead.
Reserve terminal for: builds, installs, git, processes, scripts, network, package managers, and anything that needs a shell.

Foreground (default): Commands return when done. Set timeout for long builds/scripts. Default timeout: 180 seconds, max: 600 seconds."""

READ_FILE_DESCRIPTION = """\
Read a text file with line numbers and pagination. Use this instead of cat/head/tail in terminal. \
Output format: 'LINE_NUM|CONTENT'. Suggests similar filenames if not found. \
Use offset and limit for large files. Default offset: 1, default limit: 500, max limit: 2000."""

WRITE_FILE_DESCRIPTION = """\
Write content to a file, completely replacing existing content. Use this instead of echo/cat heredoc \
in terminal. Creates parent directories automatically. OVERWRITES the entire file — use 'patch' for \
targeted edits."""

SEARCH_FILES_DESCRIPTION = """\
Search file contents or find files by name using regex/glob patterns. Use this instead of \
grep/rg/find/ls in terminal.

target='content' (default): search inside file contents with regex. Returns matching lines with \
line numbers and optional context.
target='files': search for files by name/glob pattern. Returns matching file paths.

Use file_glob to filter which files to search (e.g., '*.py'). Use output_mode to control \
output format: 'content' (matching lines), 'files_only' (file paths), 'count' (match counts)."""

PATCH_DESCRIPTION = """\
Targeted find-and-replace edits in files. Use this instead of sed/awk in terminal.

Replace mode (default): find a unique string and replace it.
Patch mode: apply V4A multi-file patches for bulk changes.

In replace mode, old_string must be unique in the file unless replace_all is true. \
Include enough surrounding context to ensure uniqueness."""


# ── Toolset ──

class HermesToolset(Toolset):
    """Session toolset exposing the Hermes Agent five-tool coding surface.

    The toolset is bound to a session by passing it to ``env.session(...)``::

        from openreward.toolsets import HermesToolset

        with env.session(task=task, toolset="hermes") as session:
            session.call_tool("terminal", {"command": "ls"})

    Requires the bound environment to define ``self.sandbox``.
    """

    @classmethod
    def name(cls) -> str:
        return "hermes"

    @tool
    async def terminal(self, params: TerminalParams) -> ToolOutput:
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
    async def read_file(self, params: ReadFileParams) -> ToolOutput:
        try:
            content = await _download_text(self.sandbox, params.path)
            lines = content.splitlines()

            start = max(0, params.offset - 1)
            end = start + params.limit
            selected_lines = lines[start:end]

            output_lines = [f"{i}|{line}" for i, line in enumerate(selected_lines, start=start + 1)]
            output = "\n".join(output_lines)

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
    async def write_file(self, params: WriteFileParams) -> ToolOutput:
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
    async def search_files(self, params: SearchFilesParams) -> ToolOutput:
        try:
            if params.target == "files":
                cmd = f"find {params.path} -type f -name '{params.pattern}'"
                output, code = await self.sandbox.run(cmd)
                if code != 0:
                    return ToolOutput(
                        metadata={"error": output, "exit_code": code},
                        blocks=[TextBlock(text=f"search_files failed (exit {code}):\n{output}")],
                        finished=False,
                    )
                lines = [l for l in output.splitlines() if l.strip()]
                lines = lines[params.offset:params.offset + params.limit]
                result = "\n".join(lines)
                return ToolOutput(
                    metadata={"output": result, "exit_code": 0},
                    blocks=[TextBlock(text=result if result else "No files found.")],
                    reward=0.0,
                    finished=False,
                )
            else:
                glob_flag = f" --include='{params.file_glob}'" if params.file_glob else ""
                context_flag = f" -C {params.context}" if params.context > 0 else ""

                if params.output_mode == "files_only":
                    mode_flag = " -l"
                elif params.output_mode == "count":
                    mode_flag = " -c"
                else:
                    mode_flag = " -n"

                cmd = f"grep -r{mode_flag}{context_flag}{glob_flag} '{params.pattern}' {params.path}"
                output, code = await self.sandbox.run(cmd)

                # grep returns 1 for no matches — not an error
                if code > 1:
                    return ToolOutput(
                        metadata={"error": output, "exit_code": code},
                        blocks=[TextBlock(text=f"search_files failed (exit {code}):\n{output}")],
                        finished=False,
                    )

                lines = output.splitlines()
                lines = lines[params.offset:params.offset + params.limit]
                result = "\n".join(lines)

                return ToolOutput(
                    metadata={"output": result, "exit_code": 0},
                    blocks=[TextBlock(text=result if result else "No matches found.")],
                    reward=0.0,
                    finished=False,
                )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error searching files: {str(e)}")],
                finished=False,
            )

    @tool
    async def patch(self, params: PatchParams) -> ToolOutput:
        if params.mode == "replace":
            return await self._patch_replace(params)
        elif params.mode == "patch":
            return await self._patch_v4a(params)
        else:
            return ToolOutput(
                metadata={"error": f"Unknown mode: {params.mode}"},
                blocks=[TextBlock(text=f"Error: unknown patch mode '{params.mode}'. Use 'replace' or 'patch'.")],
                finished=False,
            )

    async def _patch_replace(self, params: PatchParams) -> ToolOutput:
        try:
            if not params.path or params.old_string is None or params.new_string is None:
                return ToolOutput(
                    metadata={"error": "path, old_string, and new_string are required for replace mode"},
                    blocks=[TextBlock(text="Error: path, old_string, and new_string are required for replace mode.")],
                    finished=False,
                )

            content = await _download_text(self.sandbox, params.path)

            count = content.count(params.old_string)
            if count == 0:
                return ToolOutput(
                    metadata={"error": f"old_string not found in {params.path}"},
                    blocks=[TextBlock(text=f"Error: old_string not found in {params.path}")],
                    finished=False,
                )
            if not params.replace_all and count > 1:
                return ToolOutput(
                    metadata={"error": f"old_string appears {count} times; use replace_all or provide more context"},
                    blocks=[TextBlock(text=f"Error: old_string appears {count} times in {params.path}. Must be unique unless replace_all=true.")],
                    finished=False,
                )

            if params.replace_all:
                new_content = content.replace(params.old_string, params.new_string)
            else:
                new_content = content.replace(params.old_string, params.new_string, 1)

            await _upload_text(self.sandbox, params.path, new_content, ensure_trailing_newline=True)

            return ToolOutput(
                metadata={"output": "", "exit_code": 0},
                blocks=[TextBlock(text=f"Successfully patched {params.path}")],
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error patching file: {str(e)}")],
                finished=False,
            )

    async def _patch_v4a(self, params: PatchParams) -> ToolOutput:
        try:
            if not params.patch:
                return ToolOutput(
                    metadata={"error": "patch content is required for patch mode"},
                    blocks=[TextBlock(text="Error: patch content is required for patch mode.")],
                    finished=False,
                )

            patch_tmp = "/tmp/_hermes_patch.diff"
            await _upload_text(self.sandbox, patch_tmp, params.patch, ensure_trailing_newline=True)

            output, code = await self.sandbox.run(f"patch -p1 < {patch_tmp}")
            if code != 0:
                await self.sandbox.run(f"rm -f {patch_tmp}")
                return ToolOutput(
                    metadata={"error": output, "exit_code": code},
                    blocks=[TextBlock(text=f"patch failed (exit {code}):\n{output}")],
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


HermesToolset.terminal.__doc__ = TERMINAL_DESCRIPTION
HermesToolset.read_file.__doc__ = READ_FILE_DESCRIPTION
HermesToolset.write_file.__doc__ = WRITE_FILE_DESCRIPTION
HermesToolset.search_files.__doc__ = SEARCH_FILES_DESCRIPTION
HermesToolset.patch.__doc__ = PATCH_DESCRIPTION
