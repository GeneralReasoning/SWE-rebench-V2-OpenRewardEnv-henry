import asyncio
import base64
import re
import threading
from pathlib import Path
from typing import ClassVar, Mapping, Optional, Union

import aiohttp
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt
from openreward.api._session.http import request_retryable, resumable_sse
from openreward.api._session.session import BaseAsyncSession, SessionKind, SessionTerminatedError
from openreward.api.sandboxes.secrets import build_secrets_header, augment_secrets_with_api_key
from openreward.api.sandboxes.types import PodTerminatedError, RunResult, SandboxSettings

def _is_unknown_task_id(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and "unknown task_id" in str(exc)

# ECMA-48 escape sequences: CSI (Fe) sequences and single-character escapes
_ANSI_ESCAPE_RE = re.compile(r'\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# Control chars to strip: C0 (except \t \n \r), DEL, C1
_CONTROL_CHAR_TABLE = {
    c: None
    for c in (*range(0x00, 0x09), 0x0b, 0x0c, *range(0x0e, 0x20), *range(0x7f, 0xa0))
}

def _sanitise_content(content: str) -> str:
    """Sanitise a string so it is safe for JSON encoding and LLM tokenization."""
    content = content.encode("utf-8", "backslashreplace").decode("utf-8")
    content = _ANSI_ESCAPE_RE.sub("", content)
    content = content.translate(_CONTROL_CHAR_TABLE)
    return content

def _decode_output(output: str) -> str:
    """Output from the terminal is base64 encoded, as it can arbitrary binary data."""
    return base64.b64decode(output.encode('utf-8')).decode('utf-8', 'surrogateescape').rstrip()


class AsyncSandboxesAPI(BaseAsyncSession):
    _session_kind: ClassVar[SessionKind] = "sandbox"

    def __init__(
        self,
        base_url: str,
        api_key: str,
        settings: SandboxSettings,
        creation_timeout: int = 60 * 30,
        secrets: Optional[Mapping[str, Union[str, tuple[str, list[str]]]]] = None,
        api_base_url: Optional[str] = None,
    ) -> None:
        secrets = augment_secrets_with_api_key(secrets, api_key, base_url, api_base_url)
        # Build X-Secrets header if secrets provided — only sent on creation request
        creation_headers: Optional[dict[str, str]] = None
        if secrets:
            creation_headers = {"X-Secrets": build_secrets_header(secrets)}

        super().__init__(
            base_url=base_url,
            api_key=api_key,
            creation_endpoint="/create_sandbox",
            creation_payload=settings.model_dump(),
            creation_timeout=creation_timeout,
            creation_headers=creation_headers,
        )
        self.settings = settings

    def _ensure_started(self):
        if (self._external_client or self._own_client) is None:
            raise RuntimeError("Sandbox not started. Call start() or use as context manager.")

    async def run(
        self,
        cmd: str,
        timeout: Optional[float] = 300,
        max_bytes: Optional[int] = 50_000,
        sanitise: bool = True,
    ) -> RunResult:
        """Run a command in the container.

        Returns a :class:`RunResult` which supports backwards-compatible
        2-tuple unpacking::

            output, return_code = await sandbox.run("ls")

        Additional fields are available as attributes::

            result = await sandbox.run("ls")
            if result.truncated:
                ...
            if result.timed_out:
                ...
        """
        self._ensure_alive()
        self._ensure_started()

        # Request one extra byte so we can detect truncation without a false
        # positive when output is exactly max_bytes long.
        fetch_bytes = (max_bytes + 1) if max_bytes is not None else None

        # Retry once if the exec-agent restarted (e.g. OOM) and lost its in-memory task map.
        async for attempt in AsyncRetrying(
            retry=retry_if_exception(_is_unknown_task_id),
            stop=stop_after_attempt(2),
            reraise=True,
        ):
            with attempt:
                run_coro = resumable_sse(
                    self.client,
                    "/run",
                    token=self.api_key,
                    json={
                        "cmd": cmd,
                        "timeout_s": timeout,
                        "max_bytes": fetch_bytes,
                        "shell": "/bin/bash",
                    },
                    sid=self.sid,
                    max_retries=5,
                )
                res = await self._run_or_die(run_coro)

        return_code = res["return_code"]
        output = _decode_output(res["output"])
        if sanitise:
            output = _sanitise_content(output)

        truncated = max_bytes is not None and len(output) > max_bytes
        if truncated:
            output = output[:max_bytes]

        # return_code 124 is the standard Unix timeout exit code, used
        # explicitly by the exec-agent when context deadline is exceeded.
        timed_out = timeout is not None and return_code == 124

        return RunResult(
            output=output,
            return_code=return_code,
            truncated=truncated,
            timed_out=timed_out,
            sanitised=sanitise,
        )

    async def check_run(
        self,
        cmd: str,
        timeout: Optional[float] = 300,
        max_bytes: Optional[int] = 50_000,
        sanitise: bool = True,
    ) -> str:
        """Run a command in the container and raise an error if it fails."""
        self._ensure_alive()
        result = await self.run(cmd, timeout=timeout, max_bytes=max_bytes, sanitise=sanitise)
        if result.return_code != 0:
            raise RuntimeError(f"Command failed: {cmd}\n{result.output}")
        return result.output

    async def upload(self, local_path: Union[str, Path], container_path: str) -> None:
        """Upload a single file from local filesystem to the container."""
        self._ensure_alive()
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"Local file not found: {local_path}")

        max_size = 10 * 1024 * 1024
        if local_path.stat().st_size > max_size:
            raise ValueError(f"File is too large: {local_path.stat().st_size} bytes > {max_size} bytes")

        file_content = local_path.read_bytes()
        encoded_content = base64.b64encode(file_content).decode('ascii')

        cmd = f"echo '{encoded_content}' | base64 -d > {container_path}"
        await self.check_run(cmd, max_bytes=max_size)

    async def download(self, container_path: str) -> bytes:
        """Download a single file from the container."""
        self._ensure_alive()
        cmd = f"base64 {container_path}"
        output = await self.check_run(cmd, max_bytes=None)

        try:
            file_content = base64.b64decode(output.encode('ascii'))
            return file_content
        except Exception as e:
            raise RuntimeError(f"Failed to decode and write file: {e}")

    async def start(self) -> None:
        await self.__aenter__()

    async def stop(self) -> None:
        await self.__aexit__(None, None, None)



class SandboxesAPI:
    """Synchronous wrapper around AsyncSandboxesAPI.

    The event loop runs in a background daemon thread so that the ping
    task stays alive between synchronous calls (preventing session TTL
    expiry in Redis).
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        settings: SandboxSettings,
        creation_timeout: int = 60 * 30,
        secrets: Optional[Mapping[str, Union[str, tuple[str, list[str]]]]] = None,
        api_base_url: Optional[str] = None,
    ) -> None:
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._loop_thread.start()
        self._base_url = base_url
        self._api_key = api_key
        self._settings = settings
        self._creation_timeout = creation_timeout
        self._secrets = secrets
        self._api_base_url = api_base_url
        self._async: Optional[AsyncSandboxesAPI] = None

    def _run(self, coro):
        """Submit a coroutine to the background loop and block until done."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    @property
    def sid(self) -> Optional[str]:
        return self._async.sid if self._async else None

    def run(
        self,
        cmd: str,
        timeout: Optional[float] = 300,
        max_bytes: Optional[int] = 50_000,
        sanitise: bool = True,
    ) -> RunResult:
        """Run a command in the container."""
        if self._async is None:
            raise RuntimeError("Sandbox not started. Call start() or use as context manager.")
        return self._run(
            self._async.run(cmd, timeout=timeout, max_bytes=max_bytes, sanitise=sanitise)
        )

    def check_run(
        self,
        cmd: str,
        timeout: Optional[float] = 300,
        max_bytes: Optional[int] = 50_000,
        sanitise: bool = True,
    ) -> str:
        """Run a command in the container and raise an error if it fails."""
        if self._async is None:
            raise RuntimeError("Sandbox not started. Call start() or use as context manager.")
        return self._run(
            self._async.check_run(cmd, timeout=timeout, max_bytes=max_bytes, sanitise=sanitise)
        )

    def upload(self, local_path: Union[str, Path], container_path: str) -> None:
        """Upload a single file from local filesystem to the container."""
        if self._async is None:
            raise RuntimeError("Sandbox not started. Call start() or use as context manager.")
        self._run(
            self._async.upload(local_path, container_path)
        )

    def download(self, container_path: str) -> bytes:
        """Download a single file from the container."""
        if self._async is None:
            raise RuntimeError("Sandbox not started. Call start() or use as context manager.")
        return self._run(
            self._async.download(container_path)
        )

    def start(self) -> None:
        self._async = AsyncSandboxesAPI(
            base_url=self._base_url,
            api_key=self._api_key,
            settings=self._settings,
            creation_timeout=self._creation_timeout,
            secrets=self._secrets,
            api_base_url=self._api_base_url,
        )
        self._run(self._async.start())

    def stop(self) -> None:
        if self._async is not None:
            self._run(self._async.stop())

    def close(self):
        """Clean up resources."""
        self.stop()
        self._run(self._loop.shutdown_asyncgens())
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()

    def __enter__(self) -> "SandboxesAPI":
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()