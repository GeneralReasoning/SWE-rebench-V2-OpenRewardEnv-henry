from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Literal, Union

from typing_extensions import TypeAliasType

JSONValue = TypeAliasType("JSONValue", Union[Mapping[str, Any], Sequence[Any], str, int, float, bool, None])
JSONObject = Mapping[str, JSONValue]

@dataclass
class Server:
    name: str

@dataclass
class Environment:
    server_name: str
    environment_name: str
    namespace: str = "matrix"

    @property
    def deployment_name(self) -> str:
        """Get the full deployment identifier in namespace/server_name format."""
        if "/" in self.server_name:
            # Already contains namespace
            return self.server_name
        return f"{self.namespace}/{self.server_name}"

@dataclass
class Task:
    server_name: str
    environment_name: str
    task_spec: JSONObject
    namespace: Optional[str]

    @property
    def deployment_name(self) -> str:
        """Get the full deployment identifier in namespace/server_name format."""
        if self.namespace is None:
            return self.server_name
        else:
            return f"{self.namespace}/{self.server_name}"

@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: Optional[JSONObject]

@dataclass
class TextBlock:
    text: str
    detail: Optional[JSONObject] = None
    type: Literal["text"] = "text"

@dataclass
class ImageBlock:
    data: str
    mimeType: str
    detail: Optional[JSONObject] = None
    type: Literal["image"] = "image"


@dataclass
class ToolOutput:
    blocks: list[Union[TextBlock, ImageBlock]]
    metadata: Optional[JSONObject] = None
    reward: Optional[float] = None
    finished: bool = False

class ToolCallError(Exception):
    pass

from openreward.api._session.http import AuthenticationError as AuthenticationError  # noqa: F401

Provider = Literal[
    "openai",
    "anthropic",
    "google",
    "openrouter"
]