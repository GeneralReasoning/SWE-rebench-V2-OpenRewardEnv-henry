"""Pre-built toolsets for common tasks."""

from openreward.environments.toolset import Toolset

from .claude_code import ClaudeCodeToolset
from .codex import CodexToolset
from .excel import ExcelToolset
from .gemini_cli import GeminiCliToolset
from .hermes import HermesToolset
from .openclaw import OpenClawToolset
from .pdf import PDFToolset
from .powerpoint import PowerPointToolset
from .word import WordToolset

# Registry of session-scoped toolsets that can be passed by name to
# ``env.session(toolset=...)``. The session API resolves either a class or an
# instance to its registered name and forwards that name to the server, which
# instantiates the class against its per-session ``Environment``.
BUILTIN_TOOLSETS: dict[str, type[Toolset]] = {
    ClaudeCodeToolset.name(): ClaudeCodeToolset,
    CodexToolset.name(): CodexToolset,
    GeminiCliToolset.name(): GeminiCliToolset,
    HermesToolset.name(): HermesToolset,
    OpenClawToolset.name(): OpenClawToolset,
}

__all__ = [
    "BUILTIN_TOOLSETS",
    "ClaudeCodeToolset",
    "CodexToolset",
    "ExcelToolset",
    "GeminiCliToolset",
    "HermesToolset",
    "OpenClawToolset",
    "PowerPointToolset",
    "WordToolset",
    "PDFToolset",
]
