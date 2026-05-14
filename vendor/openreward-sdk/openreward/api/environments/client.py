import asyncio
import atexit
import threading
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, ClassVar, Literal, Optional, Union, overload

import aiohttp
from openreward.api._session.http import (
    _raise_for_status,
    request_retryable,
    resumable_sse,
)
from openreward.api._session.session import BaseAsyncSession, SessionKind, SessionTerminatedError

BuiltinToolset = Literal["claude-code", "codex", "gemini-cli", "hermes", "openclaw"]
_VALID_BUILTIN_TOOLSETS = {"claude-code", "codex", "gemini-cli", "hermes", "openclaw"}
from .types import (
    ImageBlock,
    JSONObject,
    JSONValue,
    Mapping,
    Provider,
    Server,
    Task,
    TextBlock,
    ToolCallError,
    ToolOutput,
    ToolSpec,
)
from openreward.api.sandboxes.secrets import build_secrets_header, augment_secrets_with_api_key

GOOGLE_UNSUPPORTED_SCHEMA_KEYS = {
    "additionalProperties",      # JSON Schema
    "additional_properties",     # sometimes appears already converted
    "title",                     # you already strip, but keep it here too
    "default",                   # often unsupported in function schemas
    "examples",
    "example",
    "patternProperties",
    "oneOf",
    "allOf",
    "anyOf",
    "not",
}

OPENAI_UNSUPPORTED_SCHEMA_KEYS = {
    "additionalProperties",
    "patternProperties",
    "oneOf",
    "allOf",
    "anyOf",
    "not",
}

def _sanitize_google_schema(x: Any) -> Any:
    """Recursively remove schema keys that Gemini/Google function calling rejects."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            if k in GOOGLE_UNSUPPORTED_SCHEMA_KEYS:
                continue
            if k == "$ref":
                k = "ref"
            elif k == "$defs":
                k = "defs"
            out[k] = _sanitize_google_schema(v)
        return out
    if isinstance(x, list):
        return [_sanitize_google_schema(i) for i in x]
    return x

def _fix_array_schemas(obj: Any) -> Any:
    """Recursively add missing 'items' to array schemas (required by OpenAI)."""
    if isinstance(obj, list):
        return [_fix_array_schemas(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    obj = {k: _fix_array_schemas(v) for k, v in obj.items()}
    t = obj.get("type")
    is_array = t == "array" or (isinstance(t, list) and "array" in t)
    if is_array and "items" not in obj:
        obj["items"] = {}
    return obj

def _sanitize_openai_schema(x: Any) -> Any:
    """
    Recursively sanitize schema for OpenAI function calling.

    - Collapses anyOf/oneOf/allOf into a single option (first non-null for
      anyOf, first entry otherwise) while preserving sibling metadata such as
      description/default/title on the enclosing schema.
    - Removes unsupported keywords (additionalProperties, patternProperties,
      not, etc.)
    - Ensures array types have 'items' field
    """
    if isinstance(x, dict):
        for key in ("anyOf", "oneOf", "allOf"):
            if key not in x:
                continue
            options = x[key]
            if not options:
                continue
            chosen = None
            if key == "anyOf":
                for option in options:
                    if not (isinstance(option, dict) and option.get("type") == "null"):
                        chosen = option
                        break
            if chosen is None:
                chosen = options[0]
            siblings = {k: v for k, v in x.items() if k != key}
            if isinstance(chosen, dict):
                merged = {**siblings, **chosen}
            else:
                return _sanitize_openai_schema(chosen)
            return _sanitize_openai_schema(merged)

        out = {}
        for k, v in x.items():
            if k in OPENAI_UNSUPPORTED_SCHEMA_KEYS:
                continue
            out[k] = _sanitize_openai_schema(v)

        if out.get("type") == "array" and "items" not in out:
            out["items"] = {}

        return out

    if isinstance(x, list):
        return [_sanitize_openai_schema(i) for i in x]

    return x

def _strip_titles(value: Any) -> Any:
    """Recursively remove JSON schema `title` keys."""
    if isinstance(value, dict):
        return {
            k: _strip_titles(v)
            for k, v in value.items()
            if k != "title"
        }
    if isinstance(value, list):
        return [_strip_titles(item) for item in value]
    return value


def sanitize_tool_schema(
    schema: Optional[Mapping[str, Any]],
    provider: Provider,
) -> dict[str, Any]:
    """Sanitize a JSON Schema for a given provider's function-calling API.

    Strips ``title`` keys and applies provider-specific fixups:

    - ``openai``/``openrouter``: collapse anyOf/oneOf/allOf, drop unsupported
      keywords (``additionalProperties``, ``patternProperties``, ``not``), and
      default missing ``items`` on array types.
    - ``anthropic``: strip titles only (Anthropic accepts standard JSON Schema).
    - ``google``: drop Gemini-unsupported keys and rename ``$ref``/``$defs``.

    Returns ``{}`` when ``schema`` is ``None`` or empty, so callers can safely
    spread the result (``ToolParams(**sanitize_tool_schema(...))``).
    """
    if not schema:
        return {}
    stripped = _strip_titles(schema)
    if provider in ("openai", "openrouter"):
        return _fix_array_schemas(_sanitize_openai_schema(stripped))
    if provider == "anthropic":
        return stripped
    if provider == "google":
        return _sanitize_google_schema(stripped)
    raise ValueError(f"Invalid provider: {provider!r}")


@overload
def convert_tool_response(res: Mapping[str, Any], format: None = None) -> list[ToolSpec]: ...

@overload
def convert_tool_response(res: Mapping[str, Any], format: Provider = ...) -> list[dict[str, Any]]: ...

def convert_tool_response(
    res: Mapping[str, Any],
    format: Optional[Provider] = None,
) -> Union[list[ToolSpec], list[dict[str, Any]]]:
    if format is None:
        return [ToolSpec(**tool) for tool in res["tools"]]

    if format not in ("openai", "openrouter", "anthropic", "google"):
        raise ValueError(f"Invalid format: {format!r}")

    out: list[dict[str, Any]] = []
    for tool in res["tools"]:
        raw_schema = tool.get("input_schema")
        sanitized = sanitize_tool_schema(raw_schema, format)
        meta = {
            k: _strip_titles(v)
            for k, v in tool.items()
            if k not in {"input_schema", "title"}
        }

        if format == "openai":
            out.append({
                "type": "function",
                **meta,
                "parameters": sanitized if raw_schema else None,
            })
        elif format == "openrouter":
            out.append({
                "type": "function",
                "function": meta,
                "parameters": sanitized if raw_schema else None,
            })
        elif format == "anthropic":
            out.append({
                "type": "custom",
                **meta,
                "input_schema": sanitized if raw_schema else {"type": "object", "properties": {}},
            })
        else:  # google
            out.append({
                **meta,
                "parameters": sanitized if raw_schema else None,
            })

    return out

def _validate_toolset_name(toolset: Optional[str]) -> Optional[str]:
    """Validate that *toolset* is a known built-in toolset name."""
    if toolset is None:
        return None
    if toolset not in _VALID_BUILTIN_TOOLSETS:
        raise ValueError(
            f"Unknown toolset {toolset!r}; "
            f"valid options: {sorted(_VALID_BUILTIN_TOOLSETS)}"
        )
    return toolset


@asynccontextmanager
async def matrix_sid_provider(client: aiohttp.ClientSession, server_name: str, token: Optional[str]) -> AsyncGenerator[str, None]:
    """Ephemeral SID provider using SSE-based /create_session, cleanup via /delete."""
    sid: Optional[str] = None

    def on_event(event: str, data: str) -> None:
        nonlocal sid
        if event == "task_id":
            sid = data.strip()

    await resumable_sse(
        client,
        "/create_session",
        token=token,
        deployment=server_name,
        max_retries=3,
        on_event=on_event,
    )

    assert sid is not None, "No SID returned from /create_session"
    try:
        yield sid
    finally:
        try:
            await request_retryable(client, "POST", "/delete_session", sid=sid, expect_json=False, token=token)
        except Exception:
            pass


class AsyncSession(BaseAsyncSession):
    _session_kind: ClassVar[SessionKind] = "environment"

    def __init__(
        self,
        env: "AsyncEnvironment",
        task: Optional[Task] = None,
        secrets: Optional[Mapping[str, Union[str, tuple[str, list[str]]]]] = None,
        api_key: Optional[str] = None,
        split: Optional[str] = None,
        index: Optional[int] = None,
        toolset_name: Optional[str] = None,
        env_overrides: Optional[Mapping[str, str]] = None,
    ):
        has_task = task is not None
        has_index = split is not None and index is not None
        if has_task == has_index:
            raise ValueError("Provide either task or both split and index, not both/neither")
        if (split is None) != (index is None):
            raise ValueError("split and index must both be provided together")

        secrets = augment_secrets_with_api_key(
            secrets, api_key,
            base_url=str(env.client._base_url),
            api_base_url=str(env.api_client._base_url) if env.api_client else None,
        )

        creation_headers: Optional[dict[str, str]] = None
        if secrets:
            creation_headers = {"X-Secrets": build_secrets_header(secrets)}

        creation_payload: dict[str, Any] = {}
        if env_overrides:
            creation_payload["env"] = dict(env_overrides)

        super().__init__(
            base_url=str(env.client._base_url),
            api_key=api_key,
            creation_endpoint="/create_session",
            creation_payload=creation_payload,
            deployment=env.deployment_name,
            client=env.client,
            creation_headers=creation_headers,
        )

        self._secrets_headers = creation_headers
        self.env = env
        self.task = task
        self.split = split
        self.index = index
        self.toolset_name = toolset_name

        self._has_task_tools: bool = True

    def _env_path(self, suffix: str) -> str:
        """Build URL path, matching AsyncEnvironment's routing pattern.

        When variant is None, uses bare path (redirect middleware handles it).
        When variant is set, prefixes with the variant name.
        """
        if self.env.variant is None:
            return suffix
        return f"/{self.env.variant}{suffix}"

    async def _post_create(self) -> None:
        """POST /create with task payload after SID is obtained."""
        create_payload: dict[str, Any] = {}
        if self.task is not None:
            create_payload["task_spec"] = self.task.task_spec
            create_payload["env_name"] = self.task.environment_name
        else:
            create_payload["split"] = self.split
            create_payload["index"] = self.index
            if self.env.variant is not None:
                create_payload["env_name"] = self.env.variant
        if self.toolset_name is not None:
            create_payload["toolset_name"] = self.toolset_name

        await request_retryable(
            self.client,
            "POST",
            "/create",
            expect_json=True,
            sid=self.sid,
            deployment=self.deployment,
            json=create_payload,
            token=self.api_key,
            extra_headers=self._secrets_headers,
        )

    async def _pre_delete(self) -> None:
        """POST /delete to tear down the environment on the server."""
        if self.sid:
            await request_retryable(
                self.client,
                "POST",
                "/delete",
                expect_json=False,
                sid=self.sid,
                token=self.api_key,
            )

    async def version(self) -> dict[str, Optional[str]]:
        return await self._run_or_die(
            request_retryable(
                self.client,
                "GET",
                "/version",
                expect_json=True,
                sid=self.sid,
                deployment=self.deployment,
                token=self.api_key,
            )
        )

    async def get_prompt(self) -> list[Union[TextBlock, ImageBlock]]:
        res = await self._run_or_die(
            request_retryable(
                self.client,
                "GET",
                self._env_path("/prompt"),
                expect_json=True,
                sid=self.sid,
                deployment=self.deployment,
                token=self.api_key,
            )
        )
        blocks: list[Union[TextBlock, ImageBlock]] = []
        for block in res:
            if block["type"] == "text":
                blocks.append(TextBlock(text=block["text"], detail=block["detail"]))
            elif block["type"] == "image":
                blocks.append(ImageBlock(mimeType=block["mimeType"], detail=block["detail"], data=block["data"]))
        return blocks

    @overload
    async def list_tools(self, format: None = None) -> list[ToolSpec]: ...

    @overload
    async def list_tools(self, format: Provider) -> list[dict]: ...

    async def list_tools(self, format: Optional[Provider] = None) -> Union[list[ToolSpec], list[dict]]:
        if self._has_task_tools:
            try:
                res = await self._run_or_die(
                    request_retryable(
                        self.client,
                        "GET",
                        self._env_path("/task_tools"),
                        expect_json=True,
                        sid=self.sid,
                        deployment=self.deployment,
                        token=self.api_key,
                    )
                )
                return convert_tool_response(res, format=format)
            except aiohttp.ClientResponseError as e:
                if e.status == 404:
                    self._has_task_tools = False
                else:
                    raise
        res = await self._run_or_die(
            request_retryable(
                self.client,
                "GET",
                self._env_path("/tools"),
                expect_json=True,
                sid=self.sid,
                deployment=self.deployment,
                token=self.api_key,
            )
        )
        return convert_tool_response(res, format=format)

    async def call_tool(self, tool_name: str, input: JSONObject = {}) -> ToolOutput:
        if not isinstance(input, Mapping):
            raise ToolCallError(f"Tool input must be a dictionary, got {type(input).__name__}")

        if not all(isinstance(k, str) for k in input.keys()):
            non_string_keys = [k for k in input.keys() if not isinstance(k, str)]
            raise ToolCallError(f"All keys in tool input must be strings. Found non-string keys: {non_string_keys}")

        res = await self._run_or_die(
            resumable_sse(
                self.client,
                self._env_path("/call"),
                sid=self.sid,
                deployment=self.deployment,
                token=self.api_key,
                json={"name": tool_name, "input": input},
                max_retries=5,
            )
        )

        if res["ok"]:
            blocks: list[Union[TextBlock, ImageBlock]] = []
            for block in res["output"]["blocks"]:
                if block["type"] == "text":
                    blocks.append(TextBlock(
                        text=block["text"],
                        detail=block["detail"]
                    ))
                elif block["type"] == "image":
                    blocks.append(ImageBlock(
                        mimeType=block["mimeType"],
                        detail=block["detail"],
                        data=block["data"]
                    ))
            return ToolOutput(
                blocks=blocks,
                metadata=res["output"]["metadata"],
                reward=res["output"]["reward"],
                finished=res["output"]["finished"]
            )
        else:
            raise ToolCallError(res["error"])


class AsyncEnvironment:

    def __init__(
        self,
        namespace: Optional[str],
        name: str,
        variant: Optional[str],
        client: aiohttp.ClientSession,
        api_key: Optional[str],
        api_client: Optional[aiohttp.ClientSession] = None,
    ) -> None:

        self.server = name
        self.namespace = namespace
        self.name = name
        self.variant = variant
        self.client = client
        self.api_key = api_key
        self.api_client = api_client

    @property
    def deployment_name(self) -> str:
        if self.namespace is None:
            return self.name
        else:
            return f"{self.namespace}/{self.name}"

    async def list_splits(self) -> list[str]:
        async with matrix_sid_provider(self.client, self.deployment_name, self.api_key) as sid:
            path = "/splits" if self.variant is None else f"/{self.variant}/splits"
            res = await request_retryable(self.client, "GET", path, expect_json=True, sid=sid, deployment=self.deployment_name, token=self.api_key)
            return [s["name"] for s in res]

    async def list_tasks(self, split: str) -> list[Task]:
        async with matrix_sid_provider(self.client, self.deployment_name, self.api_key) as sid:
            path = "/tasks" if self.variant is None else f"/{self.variant}/tasks"
            res = await request_retryable(self.client, "POST", path, expect_json=True, sid=sid, deployment=self.deployment_name, json={"split": split}, token=self.api_key)
            return [Task(server_name=self.server, environment_name=res["env_name"], task_spec=task, namespace=self.namespace) for task in res["tasks"]]

    async def num_tasks(self, split: str) -> int:
        """Get the number of tasks for a given split."""
        async with matrix_sid_provider(self.client, self.deployment_name, self.api_key) as sid:
            path = "/num_tasks" if self.variant is None else f"/{self.variant}/num_tasks"
            res = await request_retryable(self.client, "POST", path, expect_json=True, sid=sid, deployment=self.deployment_name, json={"split": split}, token=self.api_key)
            return res["num_tasks"]

    async def get_task(self, split: str, index: int) -> Task:
        """Get a single task by split and index."""
        async with matrix_sid_provider(self.client, self.deployment_name, self.api_key) as sid:
            path = "/task" if self.variant is None else f"/{self.variant}/task"
            res = await request_retryable(self.client, "POST", path, expect_json=True, sid=sid, deployment=self.deployment_name, json={"split": split, "index": index}, token=self.api_key)
            return Task(server_name=self.server, environment_name=res["env_name"], task_spec=res["task"], namespace=self.namespace)

    async def get_task_range(self, split: str, start: Optional[int] = None, stop: Optional[int] = None) -> list[Task]:
        """Get tasks for indices in range(start, stop). Supports negative and None indices."""
        async with matrix_sid_provider(self.client, self.deployment_name, self.api_key) as sid:
            path = "/task_range" if self.variant is None else f"/{self.variant}/task_range"
            payload: dict[str, Any] = {"split": split}
            if start is not None:
                payload["start"] = start
            if stop is not None:
                payload["stop"] = stop
            res = await request_retryable(self.client, "POST", path, expect_json=True, sid=sid, deployment=self.deployment_name, json=payload, token=self.api_key)
            return [Task(server_name=self.server, environment_name=res["env_name"], task_spec=task, namespace=self.namespace) for task in res["tasks"]]

    async def list_tools(self, format: Optional[Provider] = None) -> Union[list[ToolSpec], list[dict]]:
        path = "/tools" if self.variant is None else f"/{self.variant}/tools"
        async with matrix_sid_provider(self.client, self.deployment_name, self.api_key) as sid:
            res = await request_retryable(self.client, "GET", path, expect_json=True, sid=sid, deployment=self.deployment_name, token=self.api_key)
            return convert_tool_response(res, format=format)

    async def get_prompt(self, task: Task) -> str:
        async with matrix_sid_provider(self.client, task.deployment_name, self.api_key) as sid:
            path = "/prompt" if self.variant is None else f"/{self.variant}/prompt"
            res = await request_retryable(self.client, "GET", path, expect_json=True, sid=sid, deployment=task.deployment_name, token=self.api_key)
            return res

    def session(
        self,
        task: Optional[Task] = None,
        secrets: Optional[Mapping[str, Union[str, tuple[str, list[str]]]]] = None,
        *,
        split: Optional[str] = None,
        index: Optional[int] = None,
        toolset: Optional[BuiltinToolset] = None,
        env_overrides: Optional[Mapping[str, str]] = None,
    ) -> AsyncSession:
        """Create a session from a Task object or from split/index.

        ``toolset`` is the name of a built-in toolset (e.g. ``"claude-code"``
        or ``"codex"``). The session forwards the name to the server, which
        instantiates the toolset bound to the per-session environment. Tools
        defined on the bound toolset override any same-named tool from the
        environment or its declared toolsets.

        ``env_overrides`` overrides environment variables on the main
        container at session-create time. Restricted to the environment's
        owner - non-owners get a 403 from the backend.
        """
        toolset_name = _validate_toolset_name(toolset)
        return AsyncSession(
            self,
            task=task,
            secrets=secrets,
            api_key=self.api_key,
            split=split,
            index=index,
            toolset_name=toolset_name,
            env_overrides=env_overrides,
        )

    async def list_required_secrets(self) -> list[str]:
        """Get the list of secret keys required by this environment."""
        if self.api_client is None:
            raise RuntimeError("API base URL not configured; cannot fetch required secrets")
        owner = self.namespace or ""
        path = f"/v1/environments/{owner}/{self.name}/required-secrets"
        headers: dict[str, str] = {}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        async with self.api_client.get(path, headers=headers) as resp:
            await _raise_for_status(resp)
            data = await resp.json()
            return data["secrets"]


class AsyncEnvironmentsAPI:

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_base_url: Optional[str] = None,
    ):
        self.api_key = api_key

        self.base_url = base_url
        self.api_base_url = api_base_url
        self.timeout = aiohttp.ClientTimeout(total=None)

        # Lazily initialized - connector requires a running event loop
        self._connector: Optional[aiohttp.TCPConnector] = None
        self._clients: dict[str, aiohttp.ClientSession] = {}

    def _get_connector(self) -> aiohttp.TCPConnector:
        """Lazily create the connector when inside a running event loop."""
        if self._connector is None or self._connector.closed:
            self._connector = aiohttp.TCPConnector(limit=1_000_000)
        return self._connector

    def get(self, name: str, variant: Optional[str] = None, base_url: Optional[str] = None) -> AsyncEnvironment:

        parts = name.split("/", maxsplit=1)
        namespace = None
        if len(parts) == 1:
            env_name = parts[0]
        elif len(parts) == 2:
            namespace, env_name = parts
        else:
            raise RuntimeError("impossible")

        if namespace and self.api_key is None:
            raise ValueError(f"Expected api_key to be passed when accessing remote environment")

        if base_url is None:
            base_url = self.base_url

        if base_url not in self._clients:
            self._clients[base_url] = aiohttp.ClientSession(
                base_url=base_url,
                timeout=self.timeout,
                connector=self._get_connector(),
                trust_env=True,
            )
        client = self._clients[base_url]

        api_client = None
        if self.api_base_url:
            if self.api_base_url not in self._clients:
                self._clients[self.api_base_url] = aiohttp.ClientSession(
                    base_url=self.api_base_url,
                    timeout=self.timeout,
                    connector=self._get_connector(),
                    trust_env=True,
                )
            api_client = self._clients[self.api_base_url]

        return AsyncEnvironment(
            namespace=namespace,
            name=env_name,
            variant=variant,
            client=client,
            api_key=self.api_key,
            api_client=api_client,
        )

    async def aclose(self) -> None:
        """Close all aiohttp sessions and the shared connector.

        Sleeps briefly after closing to let aiohttp's connector finish its
        graceful-shutdown task (``_wait_for_close``). Without this, the task
        can be left pending when the event loop tears down, producing an
        ``ERROR Task was destroyed but it is pending!`` log at exit.
        """
        for client in self._clients.values():
            if not client.closed:
                await client.close()
        self._clients.clear()
        if self._connector is not None and not self._connector.closed:
            await self._connector.close()
            self._connector = None
        # https://docs.aiohttp.org/en/stable/client_advanced.html#graceful-shutdown
        await asyncio.sleep(0.25)

    async def __aenter__(self) -> "AsyncEnvironmentsAPI":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()


class Session:
    """Synchronous wrapper around AsyncSession."""

    def __init__(self, async_session: AsyncSession, loop: asyncio.AbstractEventLoop):
        self._async = async_session
        self._loop = loop

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    @property
    def sid(self) -> Optional[str]:
        return self._async.sid

    @property
    def task(self) -> Optional[Task]:
        return self._async.task

    def __enter__(self) -> "Session":
        self._run(self._async.__aenter__())
        return self

    def __exit__(self, *exc):
        self._run(self._async.__aexit__(*exc))

    def version(self) -> dict[str, Optional[str]]:
        return self._run(self._async.version())

    def get_prompt(self) -> list[Union[TextBlock, ImageBlock]]:
        return self._run(self._async.get_prompt())

    @overload
    def list_tools(self, format: None = None) -> list[ToolSpec]: ...

    @overload
    def list_tools(self, format: Provider) -> list[dict]: ...

    def list_tools(self, format: Optional[Provider] = None) -> Union[list[ToolSpec], list[dict]]:
        return self._run(self._async.list_tools(format))

    def call_tool(self, tool_name: str, input: JSONObject = {}) -> ToolOutput:
        return self._run(self._async.call_tool(tool_name, input))


class Environment:
    """Synchronous wrapper around AsyncEnvironment."""

    def __init__(self, async_env: AsyncEnvironment, loop: asyncio.AbstractEventLoop):
        self._async = async_env
        self._loop = loop

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    @property
    def server(self) -> str:
        return self._async.server

    @property
    def namespace(self) -> Optional[str]:
        return self._async.namespace

    @property
    def name(self) -> str:
        return self._async.name

    @property
    def variant(self) -> Optional[str]:
        return self._async.variant

    @property
    def deployment_name(self) -> str:
        return self._async.deployment_name

    def list_splits(self) -> list[str]:
        return self._run(self._async.list_splits())

    def list_tasks(self, split: str) -> list[Task]:
        return self._run(self._async.list_tasks(split))

    def num_tasks(self, split: str) -> int:
        """Get the number of tasks for a given split."""
        return self._run(self._async.num_tasks(split))

    def get_task(self, split: str, index: int) -> Task:
        """Get a single task by split and index."""
        return self._run(self._async.get_task(split, index))

    def get_task_range(self, split: str, start: Optional[int] = None, stop: Optional[int] = None) -> list[Task]:
        """Get tasks for indices in range(start, stop). Supports negative and None indices."""
        return self._run(self._async.get_task_range(split, start, stop))

    @overload
    def list_tools(self, format: None = None) -> list[ToolSpec]: ...

    @overload
    def list_tools(self, format: Provider) -> list[dict]: ...

    def list_tools(self, format: Optional[Provider] = None) -> Union[list[ToolSpec], list[dict]]:
        return self._run(self._async.list_tools(format))

    def get_prompt(self, task: Task) -> str:
        return self._run(self._async.get_prompt(task))

    def session(
        self,
        task: Optional[Task] = None,
        secrets: Optional[Mapping[str, Union[str, tuple[str, list[str]]]]] = None,
        *,
        split: Optional[str] = None,
        index: Optional[int] = None,
        toolset: Optional[BuiltinToolset] = None,
        env_overrides: Optional[Mapping[str, str]] = None,
    ) -> Session:
        """Create a session from a Task object or from split/index.

        See :meth:`AsyncEnvironment.session` for the ``toolset`` and
        ``env_overrides`` parameters.
        """
        async_session = self._async.session(
            task=task,
            secrets=secrets,
            split=split,
            index=index,
            toolset=toolset,
            env_overrides=env_overrides,
        )
        return Session(async_session, self._loop)

    def list_required_secrets(self) -> list[str]:
        """Get the list of secret keys required by this environment."""
        return self._run(self._async.list_required_secrets())


class EnvironmentsAPI:
    """Synchronous wrapper around AsyncEnvironmentsAPI.

    The event loop runs in a background daemon thread so that the ping
    task stays alive between synchronous calls.
    """

    def __init__(self, base_url: str, api_key: str, api_base_url: Optional[str] = None):
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._loop_thread.start()
        self._async = AsyncEnvironmentsAPI(base_url, api_key, api_base_url=api_base_url)
        self._closed = False
        atexit.register(self._atexit_handler)

    def _run(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def get(self, name: str, variant: Optional[str] = None, base_url: Optional[str] = None) -> Environment:
        async def _get():
            return self._async.get(name, variant, base_url)
        async_env = self._run(_get())
        return Environment(async_env, self._loop)

    def close(self):
        """Clean up resources. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if not self._loop.is_running():
            return
        try:
            self._run(self._async.aclose())
            self._run(self._loop.shutdown_asyncgens())
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        if not self._loop.is_closed():
            self._loop.close()

    def _atexit_handler(self):
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self) -> "EnvironmentsAPI":
        return self

    def __exit__(self, *exc):
        self.close()
