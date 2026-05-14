import asyncio
import dataclasses
import json
import os
import uuid
from typing import Any, Dict, Optional

import atexit
import click
import structlog
import sys
import time
from anthropic.types import MessageParam as AnthropicMessageParam
from google.genai import types as gdm_types

from openreward.models import (
    LogMessageEvent,
    RolloutConfig,
    RolloutStartedEvent,
    RolloutUpdateEvent,
    RunInfo,
    RolloutInfo,
    SendLoopConfig,
    ShutdownEvent,
)
from openreward.http_client import make_request
from .background import start_background_worker, _send_rollout_start
from .serializers.ant import serialize_anthropic_message
from .serializers.base import UploadType, base_to_normalized
from .serializers.gdm import serialize_gdm_message
from .serializers.models import NormalizedEvent
from .serializers.oai_completions import (
    OpenAIChatMessage,
    openai_completions_to_normalized,
)
from .serializers.oai_responses import serialize_openai_response

from openreward.log_utils import get_logger as _get_logger
logger = _get_logger("openreward.rollouts.rollout")

_json_logger = structlog.wrap_logger(
    structlog.PrintLogger(sys.stdout),
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ],
)

def _rollout_logging_format() -> str:
    # Read at call time so os.environ mutations before first use are respected.
    # Valid values: "pretty", "structured". Defaults to "pretty" if unset.
    return os.getenv("OPENREWARD_ROLLOUT_LOGGING_FORMAT", "pretty").lower()


async def _fetch_environment_id(send_loop_config: SendLoopConfig, owner: str, env_name: str) -> Optional[str]:
    from aiohttp import ClientSession
    from openreward._version import USER_AGENT
    async with ClientSession(base_url=send_loop_config.base_url) as session:
        result = await make_request(
            client=session,
            url=f"/v1/environments/{owner}/{env_name}",
            method="GET",
            data={},
            headers={"User-Agent": USER_AGENT},
            body=None,
            max_retries=send_loop_config.max_retries,
            backoff_base=send_loop_config.backoff_base,
            backoff_factor=send_loop_config.backoff_factor,
            backoff_cap=send_loop_config.backoff_cap,
        )
        return result.get("id")


async def _fetch_variant_id(send_loop_config: SendLoopConfig, owner: str, env_name: str, variant_name: str) -> Optional[str]:
    from aiohttp import ClientSession
    from openreward._version import USER_AGENT
    async with ClientSession(base_url=send_loop_config.base_url) as session:
        variants = await make_request(
            client=session,
            url=f"/v1/environments/{owner}/{env_name}/variants",
            method="GET",
            data={},
            headers={"User-Agent": USER_AGENT},
            body=None,
            max_retries=send_loop_config.max_retries,
            backoff_base=send_loop_config.backoff_base,
            backoff_factor=send_loop_config.backoff_factor,
            backoff_cap=send_loop_config.backoff_cap,
        )
        for v in variants:
            if v.get("name") == variant_name:
                return v.get("id")
        return None

class RolloutAPI:
    def __init__(self,
        send_loop_config: SendLoopConfig,
        shutdown_timeout: float = 10.0,
        web_base_url: str = "https://openreward.ai",
        run_info: Optional[RunInfo] = None,
    ):
        self.send_loop_config = send_loop_config
        self.shutdown_timeout = shutdown_timeout
        self.web_base_url = web_base_url
        self.run_info = run_info

        self._loop, self._in, self._thread = start_background_worker(send_loop_config)
        self._closed = False

        atexit.register(self._atexit_handler)

    def _merge_run_info(self, override: Optional[RunInfo] = None) -> Optional[RunInfo]:
        """Merge client-level run_info with per-rollout override. Non-None fields in override win."""
        if self.run_info is None:
            return override
        if override is None:
            return self.run_info
        merged = {}
        for field in dataclasses.fields(RunInfo):
            override_val = getattr(override, field.name)
            merged[field.name] = override_val if override_val is not None else getattr(self.run_info, field.name)
        return RunInfo(**merged)

    def create(
        self,
        run_name: str,
        rollout_name: Optional[str] = None,
        environment: Optional[str] = None,
        variant: Optional[str] = None,
        split: Optional[str] = None,
        metadata: Optional[dict] = None,
        task_spec: Optional[Dict[str, Any]] = None,
        run_info: Optional[RunInfo] = None,
        print_messages: bool = False,
    ) -> "Rollout":
        config = RolloutConfig(
            run_name=run_name,
            rollout_name=rollout_name,
            environment=environment,
            variant=variant,
            split=split,
            metadata=metadata,
            task_spec=task_spec,
            run_info=dataclasses.asdict(merged) if (merged := self._merge_run_info(run_info)) else None,
        )
        return Rollout(config=config, _in=self._in, _loop=self._loop, web_base_url=self.web_base_url, send_loop_config=self.send_loop_config, print_messages=print_messages)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._in.put_nowait, ShutdownEvent())
            self._thread.join(timeout=self.shutdown_timeout)
            if self._thread.is_alive():
                print("openreward: shutdown timed out, some rollout data may not have been uploaded")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _atexit_handler(self):
        # Best effort if user forgot
        try:
            self.close()
        except Exception:
            pass

class Rollout:
    def __init__(self, config: RolloutConfig, _in: asyncio.Queue, _loop: asyncio.AbstractEventLoop, web_base_url: str = "https://openreward.ai", send_loop_config: Optional[SendLoopConfig] = None, print_messages: bool = False):
        self.config = config
        self.event_id = str(uuid.uuid4())
        self._in = _in
        self._loop = _loop
        self.logged_messages = 0
        self.print_messages = print_messages

        self.environment_id: Optional[str] = None
        self.variant_id: Optional[str] = None
        if send_loop_config and config.environment and "/" in config.environment:
            owner, env_name = config.environment.split("/", 1)
            try:
                future = asyncio.run_coroutine_threadsafe(
                    _fetch_environment_id(send_loop_config, owner, env_name),
                    self._loop,
                )
                self.environment_id = future.result(timeout=10)
            except Exception:
                logger.debug("failed to fetch environment_id", environment=config.environment)

            if config.variant:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        _fetch_variant_id(send_loop_config, owner, env_name, config.variant),
                        self._loop,
                    )
                    self.variant_id = future.result(timeout=10)
                except Exception:
                    logger.debug("failed to fetch variant_id", environment=config.environment, variant=config.variant)

        # This flag gets set if the client fails to create the rollout via the api.
        self._rollouts_disabled = False
        started_event = RolloutStartedEvent.from_config(
            event_id=self.event_id,
            timestamp=int(time.time() * 1000),
            config=config,
            environment_id=self.environment_id,
            variant_id=self.variant_id,
        )
        if send_loop_config is not None:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    _send_rollout_start(send_loop_config, started_event),
                    self._loop,
                )
                future.result()
            except Exception as e:
                self._rollouts_disabled = True
                logger.warning("failed to start rollout, uploads disabled", rollout_id=self.event_id, error=str(e))
        else:
            self._rollouts_disabled = True
            logger.warning("no send_loop_config, uploads disabled", rollout_id=self.event_id)

        url = f"{web_base_url.rstrip('/')}/rollout/{self.event_id}"
        if print_messages:
            if _rollout_logging_format() == "pretty":
                prefix = click.style("openreward:", bold=True, fg="blue")
                recording = click.style("Recording rollout", fg=(247, 230, 204))
                styled_url = click.style(url, fg="blue", underline=True)
                click.echo(f"{prefix} \U0001f680 {recording} {styled_url}", err=True)
            elif _rollout_logging_format() == "structured":
                _json_logger.info("rollout_started", rollout_id=self.event_id, url=url)

    def _print_message(self, normalized_event: NormalizedEvent, reward: Optional[float] = None, is_finished: Optional[bool] = False):
        BLUE = "\033[34m"
        GREEN = "\033[32m"
        YELLOW = "\033[33m"
        CYAN = "\033[36m"
        DIM = "\033[2m"
        BOLD = "\033[1m"
        RESET = "\033[0m"

        def _print_line(char="─"):
            print(f"{DIM}{char * 80}{RESET}")

        def _decode_newlines(s: str) -> str:
            return s.replace("\\n", "\n")

        t = normalized_event.type

        if t == "reasoning":
            _print_line()
            print(f"{BOLD}{DIM}💭 Reasoning{RESET}")
            _print_line()
            if normalized_event.summary:
                print(f"\n  {DIM}{_decode_newlines(normalized_event.summary)}{RESET}")
            print()
        elif t == "tool_call":
            _print_line()
            print(f"{YELLOW}⚡ Tool Call:{RESET} {BOLD}{normalized_event.name}{RESET}  {DIM}(call_id: {normalized_event.call_id}){RESET}")
            _print_line()
            if normalized_event.content:
                try:
                    args_str = json.dumps(json.loads(normalized_event.content), indent=2)
                except (json.JSONDecodeError, TypeError):
                    args_str = _decode_newlines(normalized_event.content)
                for line in args_str.splitlines():
                    print(line)
            print()
        elif t == "tool_result":
            _print_line()
            status = f"{GREEN}✓ done{RESET}" if is_finished else f"{CYAN}… continuing{RESET}"
            reward_str = f"  {DIM}reward={reward}{RESET}" if reward is not None else ""
            print(f"{GREEN}↩ Tool Result:{RESET}  {DIM}(call_id: {normalized_event.call_id}){RESET}  [{status}{reward_str}]")
            _print_line()
            if normalized_event.content:
                try:
                    args_str = json.dumps(json.loads(normalized_event.content), indent=2)
                except (json.JSONDecodeError, TypeError):
                    args_str = _decode_newlines(normalized_event.content)
                for line in args_str.splitlines():
                    print(line)
            print()
        elif t == "assistant_message":
            _print_line()
            print(f"{BOLD}{BLUE}🤖 Assistant{RESET}")
            _print_line()
            if normalized_event.content:
                print(f"\n{_decode_newlines(normalized_event.content)}")
            print()
        elif t == "user_message":
            _print_line()
            print(f"{BOLD}👤 User{RESET}")
            _print_line()
            if normalized_event.content:
                print(f"\n{_decode_newlines(normalized_event.content)}")
            print()
        elif t == "system_message":
            _print_line()
            print(f"{DIM}⚙ System{RESET}")
            _print_line()
            if normalized_event.content:
                print(f"\n{DIM}{_decode_newlines(normalized_event.content)}{RESET}")
            print()

    def _log_message(self, normalized_event: NormalizedEvent, reward: Optional[float] = None, is_finished: Optional[bool] = False, metadata: Optional[dict] = None, rollout_info: Optional[RolloutInfo] = None):
        if getattr(self, '_rollouts_disabled', False):
            return

        if self.print_messages:
            if _rollout_logging_format() == "pretty":
                self._print_message(normalized_event, reward, is_finished)
            elif _rollout_logging_format() == "structured":
                _json_logger.info(
                    "rollout_message",
                    rollout_id=self.event_id,
                    type=normalized_event.type,
                    name=normalized_event.name,
                    call_id=normalized_event.call_id,
                    summary=normalized_event.summary,
                    reward=reward,
                    isFinished=is_finished,
                )

        if is_finished is None:
            is_finished = False

        # If rollout_info is provided, send an update event so the
        # backend merges it onto the rollout record.
        if rollout_info is not None:
            self._loop.call_soon_threadsafe(self._in.put_nowait, RolloutUpdateEvent(
                eventId=self.event_id,
                timestamp=int(time.time() * 1000),
                rollout_info=dataclasses.asdict(rollout_info),
            ))

        self._loop.call_soon_threadsafe(self._in.put_nowait, LogMessageEvent(
            eventId = str(uuid.uuid4()),
            timestamp=int(time.time() * 1000),
            index=self.logged_messages,
            rolloutEventId=self.event_id,
            environment_id=self.environment_id,
            type=normalized_event.type,
            content=normalized_event.content,
            contentReference=normalized_event.content_reference,
            summary=normalized_event.summary,
            name=normalized_event.name,
            callId=normalized_event.call_id,
            reward=reward,
            isFinished=is_finished,
            metadata=metadata or {},
        ))
        self.logged_messages += 1

    def log(
        self,
        message: UploadType,
        reward: Optional[float] = None,
        is_finished: Optional[bool] = False,
        metadata: Optional[dict] = None,
        rollout_info: Optional[RolloutInfo] = None,
    ):
        normalized_event = base_to_normalized([message])[0]
        self._log_message(normalized_event, reward, is_finished, metadata, rollout_info)

    def log_openai_completions(
        self,
        message: OpenAIChatMessage,
        reward: Optional[float] = None,
        is_finished: Optional[bool] = False,
        metadata: Optional[dict] = None,
        rollout_info: Optional[RolloutInfo] = None,
    ):
        normalized_event = openai_completions_to_normalized([message])[0]
        self._log_message(normalized_event, reward, is_finished, metadata, rollout_info)

    def log_openai_response(
        self,
        message,  # Accept both Response and ResponseInputItemParam
        reward: Optional[float] = None,
        is_finished: Optional[bool] = False,
        metadata: Optional[dict] = None,
        rollout_info: Optional[RolloutInfo] = None,
    ):
        # Handle Response objects by iterating over output
        messages_to_log = []
        if hasattr(message, 'output'):
            messages_to_log = list(message.output)
        else:
            messages_to_log = [message]

        for msg in messages_to_log:
            event = serialize_openai_response(msg)
            if event:  # Skip None (shouldn't happen with fixes, but safe)
                self._log_message(event, reward, is_finished, metadata, rollout_info)

    def log_anthropic_message(
        self,
        message: AnthropicMessageParam,
        reward: Optional[float] = None,
        is_finished: Optional[bool] = False,
        metadata: Optional[dict] = None,
        rollout_info: Optional[RolloutInfo] = None,
    ):
        for event in serialize_anthropic_message(message):
            self._log_message(event, reward, is_finished, metadata, rollout_info)

    def log_gdm_message(
        self,
        message: gdm_types.Content,
        reward: Optional[float] = None,
        is_finished: Optional[bool] = False,
        metadata: Optional[dict] = None,
        rollout_info: Optional[RolloutInfo] = None,
    ):
        for event in serialize_gdm_message(message):
            self._log_message(event, reward, is_finished, metadata, rollout_info)
