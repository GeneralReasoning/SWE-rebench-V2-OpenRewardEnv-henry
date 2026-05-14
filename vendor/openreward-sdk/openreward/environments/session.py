"""Session-scoped helpers for merging an Environment's tools with a per-session Toolset.

These helpers live alongside ``Environment`` so that the Environment class itself
stays unaware of session-scoped toolsets. The server holds the per-session
``Toolset`` instance separately and passes it through these helpers when
serving ``/{env_name}/task_tools`` and ``/{env_name}/call`` requests.
"""

from __future__ import annotations

import time
from typing import Optional

from openreward.log_utils import get_logger as _get_logger

from .environment import Environment, _introspect_tool
from .toolset import Toolset
from .types import JSONObject, ListToolsOutput, RunToolOutput, ToolSpec

logger = _get_logger("openreward.environments.session")


def _toolset_specs(toolset: Toolset) -> list[ToolSpec]:
    """Introspect a Toolset instance into ToolSpecs.

    Mirrors ``Environment.list_tools`` discovery: walks the instance for
    methods marked by ``@tool`` (and ``shared=True``), pulls schemas from
    Pydantic param models, and uses each function's docstring as the
    description.
    """
    out: list[ToolSpec] = []
    for attr in dir(toolset):
        fn = getattr(toolset, attr, None)
        if fn is None or not Environment._is_tool(fn) or not getattr(fn, "_env_tool_shared", True):
            continue
        _, hints, params = _introspect_tool(fn)
        schema = None
        if params:
            mdl = hints[params[0].name]
            schema = mdl.model_json_schema() if hasattr(mdl, "model_json_schema") else mdl.schema()  # type: ignore[attr-defined]
        out.append(ToolSpec(
            name=attr,
            description=(fn.__doc__ or "").strip(),
            input_schema=schema,
        ))
    return out


def list_session_tools(env: Environment, toolset: Optional[Toolset]) -> ListToolsOutput:
    """Return the merged tool list visible to a live session.

    Combines class-level shared tools, instance-level task tools, and any
    session-bound toolset. Tools from the session toolset replace any
    same-named tool from the environment or its declared toolsets, and a
    warning is logged for each shadow.
    """
    merged: list[ToolSpec] = list(env.list_tools().tools) + list(env.list_task_tools().tools)
    if toolset is None:
        return ListToolsOutput(tools=merged)

    ts_specs = _toolset_specs(toolset)
    ts_names = {s.name for s in ts_specs}
    for spec in merged:
        if spec.name in ts_names:
            logger.warning(
                "session_toolset_shadows_env_tool",
                tool=spec.name,
                toolset=type(toolset).__name__,
                env=type(env).__name__,
            )
    return ListToolsOutput(tools=[s for s in merged if s.name not in ts_names] + ts_specs)


async def call_session_tool(
    env: Environment,
    toolset: Optional[Toolset],
    name: str,
    input: JSONObject,
) -> RunToolOutput:
    """Dispatch a tool call for a live session.

    The session toolset takes precedence over both environment-level tools
    and class-level declared toolsets when names collide. A warning is
    logged whenever a session toolset tool shadows an env-defined tool.
    """
    if toolset is not None:
        ts_fn = getattr(toolset, name, None)
        if ts_fn is not None and Environment._is_tool(ts_fn):
            env_fn = getattr(env, name, None)
            if env_fn is not None and Environment._is_tool(env_fn):
                logger.warning(
                    "session_toolset_shadows_env_tool",
                    tool=name,
                    toolset=type(toolset).__name__,
                    env=type(env).__name__,
                )
            return await env._invoke_tool_fn(name, ts_fn, input, time.monotonic())
    return await env._call_tool(name, input)
