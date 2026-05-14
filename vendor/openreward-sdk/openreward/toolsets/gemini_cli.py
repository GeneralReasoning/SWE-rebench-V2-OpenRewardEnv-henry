"""Gemini CLI session toolset.

Provides the built-in tools that the Gemini CLI exposes
(``run_shell_command``, ``glob``, ``grep_search``, ``read_file``,
``write_file``, ``replace``, ``list_directory``, ``write_todos``),
each backed by ``self.sandbox`` from the bound environment.

Tool descriptions and parameter schemas match the upstream Gemini CLI
source (``google-gemini/gemini-cli/packages/core/src/tools``).
"""
from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from openreward.environments import TextBlock, Toolset, ToolOutput, tool


# ── Pydantic parameter models ──


class RunShellCommandParams(BaseModel, extra="forbid"):
    command: str
    description: str = ""
    dir_path: Optional[str] = None
    is_background: bool = False
    delay_ms: Optional[float] = None


class GlobParams(BaseModel, extra="forbid"):
    pattern: str
    dir_path: Optional[str] = None


class GrepSearchParams(BaseModel, extra="forbid"):
    pattern: str
    dir_path: Optional[str] = None
    include_pattern: Optional[str] = None
    exclude_pattern: Optional[str] = None
    names_only: bool = False
    max_matches_per_file: Optional[int] = None
    total_max_matches: Optional[int] = None


class ReadFileParams(BaseModel, extra="forbid"):
    file_path: str
    start_line: Optional[int] = None
    end_line: Optional[int] = None


class WriteFileParams(BaseModel, extra="forbid"):
    file_path: str
    content: str


class ReplaceParams(BaseModel, extra="forbid"):
    file_path: str
    old_string: str
    new_string: str
    allow_multiple: bool = False
    instruction: Optional[str] = None


class ListDirectoryParams(BaseModel, extra="forbid"):
    dir_path: str
    ignore: Optional[List[str]] = None


class TodoItem(BaseModel, extra="forbid"):
    description: str
    status: Literal["pending", "in_progress", "completed", "cancelled", "blocked"]


class WriteTodosParams(BaseModel, extra="forbid"):
    todos: List[TodoItem]


# ── Sandbox text helpers ──


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


# ── Tool descriptions (verbatim from upstream Gemini CLI source) ──
# Source: packages/core/src/tools/definitions/model-family-sets/default-legacy.ts
# Shell description: packages/core/src/tools/definitions/dynamic-declaration-helpers.ts

RUN_SHELL_COMMAND_DESCRIPTION = """\
This tool executes a given shell command as `bash -c <command>`."""

GLOB_DESCRIPTION = """\
Efficiently finds files matching specific glob patterns (e.g., `src/**/*.ts`, `**/*.md`), returning absolute paths sorted by modification time (newest first)."""

GREP_SEARCH_DESCRIPTION = """\
Searches for a regular expression pattern within file contents. Max 100 matches."""

READ_FILE_DESCRIPTION = """\
Reads and returns the content of a specified file. If the file is large, the content will be truncated. The tool's response will clearly indicate if truncation has occurred."""

WRITE_FILE_DESCRIPTION = """\
Writes content to a specified file in the local filesystem. The user has the ability to modify `content`. If modified, this will be stated in the response."""

REPLACE_DESCRIPTION = """\
Replaces text within a file. By default, the tool expects to find and replace exactly ONE occurrence of `old_string`. If you want to replace multiple occurrences, set `allow_multiple` to true."""

LIST_DIRECTORY_DESCRIPTION = """\
Lists the names of files and subdirectories directly within a specified directory path. Can optionally ignore entries matching provided glob patterns."""

WRITE_TODOS_DESCRIPTION = """\
This tool helps you list out current subtasks required to complete a user request. The list of subtasks helps organize complex queries and ensure no steps are missed."""


# ── Toolset ──


class GeminiCliToolset(Toolset):
    """Session toolset exposing the Gemini CLI tool surface.

    The toolset is bound to a session by passing it to ``env.session(...)``::

        from openreward.toolsets import GeminiCliToolset

        with env.session(task=task, toolset=GeminiCliToolset()) as session:
            session.call_tool("run_shell_command", {"command": "ls"})

    Requires the bound environment to define ``self.sandbox``.
    """

    @classmethod
    def name(cls) -> str:
        return "gemini-cli"

    def __init__(self, env: Optional[Any] = None, sandbox_attr: str = "sandbox"):
        super().__init__(env, sandbox_attr)
        self.todos: List[TodoItem] = []

    @tool
    async def run_shell_command(self, params: RunShellCommandParams) -> ToolOutput:
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
    async def glob(self, params: GlobParams) -> ToolOutput:
        try:
            search_path = params.dir_path or "."
            cmd = f"find {search_path} -name '{params.pattern}' -type f | sort"
            output, code = await self.sandbox.run(cmd)
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"{output}\n\n(exit {code})")],
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error in glob search: {str(e)}")],
                finished=False,
            )

    @tool
    async def grep_search(self, params: GrepSearchParams) -> ToolOutput:
        try:
            search_path = params.dir_path or "."
            cmd_parts = ["grep", "-r", "-n"]

            if params.include_pattern:
                cmd_parts.extend(["--include", f"'{params.include_pattern}'"])

            if params.names_only:
                cmd_parts.append("-l")

            if params.max_matches_per_file is not None:
                cmd_parts.extend(["-m", str(params.max_matches_per_file)])

            cmd_parts.append(f"'{params.pattern}'")
            cmd_parts.append(search_path)

            cmd = " ".join(cmd_parts)

            if params.total_max_matches is not None:
                cmd += f" | head -n {params.total_max_matches}"

            output, code = await self.sandbox.run(cmd)
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"{output}\n\n(exit {code})")],
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error in grep search: {str(e)}")],
                finished=False,
            )

    @tool
    async def read_file(self, params: ReadFileParams) -> ToolOutput:
        try:
            if params.start_line and params.end_line:
                cmd = f"sed -n '{params.start_line},{params.end_line}p' {params.file_path} | cat -n"
                output, code = await self.sandbox.run(cmd)
            elif params.start_line:
                cmd = f"tail -n +{params.start_line} {params.file_path} | cat -n"
                output, code = await self.sandbox.run(cmd)
            elif params.end_line:
                cmd = f"head -n {params.end_line} {params.file_path} | cat -n"
                output, code = await self.sandbox.run(cmd)
            else:
                content = await _download_text(self.sandbox, params.file_path)
                lines = content.splitlines()
                output = "\n".join(
                    f"{idx + 1}\t{line}" for idx, line in enumerate(lines)
                )
                if content.endswith("\n") and output:
                    output += "\n"
                code = 0
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"{output}\n\n(exit {code})")],
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
            dir_name = os.path.dirname(params.file_path)
            if dir_name:
                await self.sandbox.run(f"mkdir -p {dir_name}")
            await _upload_text(
                self.sandbox,
                params.file_path,
                params.content,
                ensure_trailing_newline=True,
            )
            return ToolOutput(
                metadata={"output": "", "exit_code": 0},
                blocks=[
                    TextBlock(
                        text=f"Successfully wrote to {params.file_path}\n\n(exit 0)"
                    )
                ],
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
    async def replace(self, params: ReplaceParams) -> ToolOutput:
        try:
            content = await _download_text(self.sandbox, params.file_path)

            count = content.count(params.old_string)
            if count == 0:
                return ToolOutput(
                    metadata={"error": f"String not found in {params.file_path}"},
                    blocks=[
                        TextBlock(
                            text=f"Error: old_string not found in {params.file_path}"
                        )
                    ],
                    finished=False,
                )
            if not params.allow_multiple and count > 1:
                return ToolOutput(
                    metadata={
                        "error": f"old_string appears {count} times; use allow_multiple or provide more context"
                    },
                    blocks=[
                        TextBlock(
                            text=f"Error: old_string appears {count} times in {params.file_path}. Must be unique unless allow_multiple=true."
                        )
                    ],
                    finished=False,
                )

            if params.allow_multiple:
                new_content = content.replace(params.old_string, params.new_string)
            else:
                new_content = content.replace(params.old_string, params.new_string, 1)

            await _upload_text(
                self.sandbox, params.file_path, new_content, ensure_trailing_newline=True
            )

            return ToolOutput(
                metadata={"output": "", "exit_code": 0},
                blocks=[
                    TextBlock(
                        text=f"Successfully edited {params.file_path}\n\n(exit 0)"
                    )
                ],
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
    async def list_directory(self, params: ListDirectoryParams) -> ToolOutput:
        try:
            cmd = f"ls -la {params.dir_path}"
            output, code = await self.sandbox.run(cmd)
            return ToolOutput(
                metadata={"output": output, "exit_code": code},
                blocks=[TextBlock(text=f"{output}\n\n(exit {code})")],
                reward=0.0,
                finished=False,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error listing directory: {str(e)}")],
                finished=False,
            )

    @tool
    def write_todos(self, params: WriteTodosParams) -> ToolOutput:
        try:
            self.todos = params.todos

            output_lines = ["=== TODO LIST ==="]
            for todo in self.todos:
                output_lines.append(f"[{todo.status}] {todo.description}")

            text = "\n".join(output_lines)
            return ToolOutput(
                metadata={
                    "todos": [t.model_dump() for t in self.todos],
                    "count": len(self.todos),
                },
                blocks=[TextBlock(text=text)],
                finished=False,
                reward=0.0,
            )
        except Exception as e:
            return ToolOutput(
                metadata={"error": str(e)},
                blocks=[TextBlock(text=f"Error managing todos: {str(e)}")],
                finished=False,
            )


# Assign descriptions onto each tool method's __doc__ so the framework's
# introspection (which reads fn.__doc__ for ToolSpec.description) picks them up.
GeminiCliToolset.run_shell_command.__doc__ = RUN_SHELL_COMMAND_DESCRIPTION
GeminiCliToolset.glob.__doc__ = GLOB_DESCRIPTION
GeminiCliToolset.grep_search.__doc__ = GREP_SEARCH_DESCRIPTION
GeminiCliToolset.read_file.__doc__ = READ_FILE_DESCRIPTION
GeminiCliToolset.write_file.__doc__ = WRITE_FILE_DESCRIPTION
GeminiCliToolset.replace.__doc__ = REPLACE_DESCRIPTION
GeminiCliToolset.list_directory.__doc__ = LIST_DIRECTORY_DESCRIPTION
GeminiCliToolset.write_todos.__doc__ = WRITE_TODOS_DESCRIPTION
