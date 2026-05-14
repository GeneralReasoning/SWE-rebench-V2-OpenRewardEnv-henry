import asyncio
import json as json_lib
from typing import Any, AsyncGenerator, Callable, Optional, Tuple

import aiohttp
from openreward._version import USER_AGENT
from openreward.log_utils import get_logger
from tenacity import (retry, retry_if_exception, stop_after_attempt,
                      wait_exponential)

logger = get_logger("openreward-session-client")


def _is_retryable_http_error(exception: BaseException) -> bool:
    if isinstance(exception, (aiohttp.ClientError, asyncio.TimeoutError)):
        return True  # Network/timeout errors
    if isinstance(exception, aiohttp.ClientResponseError):
        return exception.status >= 500 or exception.status == 429
    return False


class AuthenticationError(Exception):
    """Raised when API authentication fails (401 Unauthorized)"""
    pass


async def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
    """Raise ClientResponseError with server's detail message if available."""
    if resp.ok:
        return
    text = await resp.text()
    try:
        detail = json_lib.loads(text).get("detail", text)
        if 'Deployment not found.' in detail:
            detail = 'Deployment not found. Environment name is case-sensitive, is it correct?'
    except Exception:
        detail = text
    raise aiohttp.ClientResponseError(
        resp.request_info, resp.history,
        status=resp.status,
        message=detail,
        headers=resp.headers,
    )


async def _raise_for_status_with_auth(resp: aiohttp.ClientResponse) -> None:
    """Like _raise_for_status but with a friendly message for 401s."""
    if resp.ok:
        return
    if resp.status == 401:
        text = await resp.text()
        RED = "\033[38;2;247;230;204m"
        BOLD = "\033[1m"
        RESET = "\033[0m"
        raise AuthenticationError(
            f"\n\n{RED}{BOLD}"
            "═══════════════════════════════════════════════════════════════\n"
            "  Authentication Failed: Missing or Invalid API Key\n"
            "═══════════════════════════════════════════════════════════════"
            f"{RESET}\n\n"
            "Your request was rejected because:\n"
            f"  • {text}\n\n"
            "To fix this:\n"
            "  1. Get your API key from: https://openreward.ai/keys\n"
            "  2. Set it as an environment variable:\n"
            "     export OPENREWARD_API_KEY='your-api-key-here'\n"
            "  3. Or pass it directly to the client:\n"
            "     client = AsyncOpenReward(api_key='your-api-key-here')\n"
        )
    await _raise_for_status(resp)


@retry(
    retry=retry_if_exception(_is_retryable_http_error),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    stop=stop_after_attempt(3),
    reraise=True,
)
async def request_retryable(
    client: aiohttp.ClientSession,
    method: str,
    path: str,
    expect_json: bool,
    token: Optional[str],
    json: Optional[dict[str, Any]] = None,
    sid: Optional[str] = None,
    deployment: Optional[str] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> Any:
    headers: dict[str, str] = {"User-Agent": USER_AGENT}
    if token is not None:
        headers["X-API-Key"] = token
    if sid:
        headers["X-Session-ID"] = sid
    if deployment:
        headers["X-Deployment"] = deployment
    if extra_headers:
        headers.update(extra_headers)

    async with client.request(method, path, headers=headers, json=json) as response:
        await _raise_for_status_with_auth(response)
        return await response.json() if expect_json else None


class HeartbeatTimeoutError(Exception):
    pass


class MaxRetriesError(Exception):
    def __init__(self, message: str, errors: Optional[list[Exception]] = None):
        self.errors = errors or []
        detail = f"{message}\n  Encountered {len(self.errors)} error(s):"
        for i, err in enumerate(self.errors, 1):
            detail += f"\n    [{i}] {type(err).__name__}: {err}"
        super().__init__(detail)


async def _parse_sse_events(
    response: aiohttp.ClientResponse,
) -> AsyncGenerator[Tuple[str, str], None]:
    """Parses an aiohttp response stream and yields SSE events."""
    event = None
    data_lines: list[str] = []
    async for raw_line in response.content:
        line = raw_line.decode("utf-8", "ignore").rstrip("\r\n")

        if not line:
            if event:
                yield event, "\n".join(data_lines)
            event = None
            data_lines = []
            continue

        if line.startswith(":"):
            continue

        field, value = line.split(":", 1)
        value = value.lstrip()

        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)


async def resumable_sse(
    client: aiohttp.ClientSession,
    path: str,
    token: Optional[str],
    *,
    json: Optional[dict[str, Any]] = None,
    sid: Optional[str] = None,
    deployment: Optional[str] = None,
    task_id: Optional[str] = None,
    max_retries: Optional[int] = None,
    backoff_base: float = 0.5,
    backoff_max: float = 10.0,
    timeout: Optional[float] = None,
    heartbeat_timeout: int = 30,
    on_event: Callable[[str, str], None] = lambda _event, _data: None,
    extra_headers: Optional[dict[str, str]] = None,
) -> Any:

    client_timeout = aiohttp.ClientTimeout(total=None, sock_read=heartbeat_timeout)
    payload = dict(json or {})
    headers: dict[str, str] = {
        "Accept": "text/event-stream",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["X-API-Key"] = token
    if sid:
        headers["X-Session-ID"] = sid
    if deployment:
        headers["X-Deployment"] = deployment
    if extra_headers:
        headers.update(extra_headers)

    async def _execute_with_retries():
        nonlocal task_id, payload
        attempt = 0
        retry_errors: list[Exception] = []
        while True:
            if task_id:
                payload["task_id"] = task_id

            try:
                async with client.post(path, headers=headers, json=payload, timeout=client_timeout) as resp:
                    await _raise_for_status_with_auth(resp)
                    attempt = 0

                    chunks = []
                    async for event, data in _parse_sse_events(resp):
                        on_event(event, data)
                        if event == "task_id":
                            task_id = data.strip()
                        elif event == "chunk":
                            chunks.append(data)
                        elif event == "end":
                            chunks.append(data)
                            final_result = "".join(chunks)
                            if not final_result:
                                return None
                            return json_lib.loads(final_result)
                        elif event == "error":
                            raise RuntimeError(data or "Unknown SSE error")

                    raise aiohttp.ClientPayloadError("Stream ended unexpectedly")

            except aiohttp.ClientResponseError as e:
                if e.status != 429 and e.status < 500:
                    raise e
                retry_errors.append(e)

            except aiohttp.ClientError as e:
                logger.debug("client_error: %s", e)
                retry_errors.append(e)

            except RuntimeError as e:
                raise e

            except asyncio.TimeoutError:
                raise HeartbeatTimeoutError()

            attempt += 1
            if max_retries is not None and attempt > max_retries:
                raise MaxRetriesError(
                    f"Exceeded {max_retries} retries for {path}",
                    errors=retry_errors,
                )

            delay = min(backoff_max, backoff_base * (2 ** (attempt - 1)))
            await asyncio.sleep(delay)

    try:
        return await asyncio.wait_for(_execute_with_retries(), timeout=timeout)
    except asyncio.TimeoutError:
        raise TimeoutError(f"Total operation timed out after {timeout} seconds.") from None


def _finalize_session(session: aiohttp.ClientSession):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            asyncio.run(session.close())
        except RuntimeError:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(session.close())
            loop.close()
    else:
        if not session.closed:
            loop.create_task(session.close())
