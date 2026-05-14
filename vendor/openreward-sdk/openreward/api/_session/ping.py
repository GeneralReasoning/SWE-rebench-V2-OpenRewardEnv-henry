import asyncio
import atexit
import concurrent.futures
import logging
import threading
import time
from dataclasses import dataclass
from typing import Literal, Optional

import aiohttp
from openreward.api._session.http import request_retryable

logger = logging.getLogger(__name__)

_SLOW_PING_THRESHOLD = 30.0

# Number of consecutive ping failures before BaseAsyncSession._run_ping
# marks the session dead client-side. Default 5 — at the standard 10s
# ping cadence this is ~50s of sustained failures, well below the
# server's SessionTTL (120s) so death is still detected before any
# real call would fail. Set higher for more tolerance, lower for
# faster client-side detection.
_MAX_CONSECUTIVE_PING_FAILURES = 5


@dataclass
class ErrorResponse:
    type: Literal["error"]
    message: str


def _make_trace_config() -> aiohttp.TraceConfig:
    """Per-request phase timing logs for the ping ClientSession.

    Fires one log line per phase (request_start, dns_start/end,
    connect_start/end, connect_reuse, request_end, request_exception) for
    every individual HTTP request — including each tenacity retry inside
    ``request_retryable``. Use these to see which phase eats the 30s
    ClientTimeout budget when a ping times out.
    """
    tc = aiohttp.TraceConfig()

    def _sid(params) -> str:
        try:
            return params.headers.get("X-Session-ID", "?")
        except Exception:
            return "?"

    async def on_request_start(session, ctx, params):
        ctx.t_start = time.monotonic()
        ctx.req_id = f"{id(ctx) & 0xFFFF:04x}"
        logger.info(
            "ping_trace req=%s start sid=%s url=%s",
            ctx.req_id, _sid(params), params.url,
        )

    async def on_dns_resolvehost_start(session, ctx, params):
        ctx.t_dns_start = time.monotonic()
        logger.info(
            "ping_trace req=%s dns_start host=%s t=%.3fs",
            ctx.req_id, params.host, ctx.t_dns_start - ctx.t_start,
        )

    async def on_dns_resolvehost_end(session, ctx, params):
        now = time.monotonic()
        logger.info(
            "ping_trace req=%s dns_end host=%s dns_elapsed=%.3fs t=%.3fs",
            ctx.req_id, params.host,
            now - getattr(ctx, "t_dns_start", ctx.t_start),
            now - ctx.t_start,
        )

    async def on_connection_create_start(session, ctx, params):
        ctx.t_conn_start = time.monotonic()
        logger.info(
            "ping_trace req=%s connect_start t=%.3fs",
            ctx.req_id, ctx.t_conn_start - ctx.t_start,
        )

    async def on_connection_create_end(session, ctx, params):
        now = time.monotonic()
        logger.info(
            "ping_trace req=%s connect_end conn_elapsed=%.3fs t=%.3fs",
            ctx.req_id,
            now - getattr(ctx, "t_conn_start", ctx.t_start),
            now - ctx.t_start,
        )

    async def on_connection_reuseconn(session, ctx, params):
        logger.info(
            "ping_trace req=%s connect_reuse t=%.3fs",
            ctx.req_id, time.monotonic() - ctx.t_start,
        )

    async def on_request_end(session, ctx, params):
        logger.info(
            "ping_trace req=%s end status=%d total=%.3fs",
            ctx.req_id, params.response.status,
            time.monotonic() - ctx.t_start,
        )

    async def on_request_exception(session, ctx, params):
        logger.warning(
            "ping_trace req=%s exception total=%.3fs error_type=%s error=%s",
            ctx.req_id, time.monotonic() - ctx.t_start,
            type(params.exception).__name__, params.exception,
        )

    tc.on_request_start.append(on_request_start)
    tc.on_dns_resolvehost_start.append(on_dns_resolvehost_start)
    tc.on_dns_resolvehost_end.append(on_dns_resolvehost_end)
    tc.on_connection_create_start.append(on_connection_create_start)
    tc.on_connection_create_end.append(on_connection_create_end)
    tc.on_connection_reuseconn.append(on_connection_reuseconn)
    tc.on_request_end.append(on_request_end)
    tc.on_request_exception.append(on_request_exception)

    return tc


async def ping(url: str, sid: str, api_key: Optional[str], sleep_time: float, client: aiohttp.ClientSession, deployment: Optional[str] = None, max_consecutive_failures: int = _MAX_CONSECUTIVE_PING_FAILURES) -> None:
    """Keepalive ping loop. Successful pings reset the consecutive-failure
    counter; only raise after ``max_consecutive_failures`` back-to-back
    failures with no intervening success.
    """
    consecutive_failures = 0
    cycle = 0
    while True:
        cycle += 1
        start = time.monotonic()
        logger.info("ping_cycle_start sid=%s cycle=%d", sid, cycle)
        try:
            await request_retryable(
                client,
                "POST",
                url,
                sid=sid,
                deployment=deployment,
                expect_json=False,
                token=api_key,
            )
            consecutive_failures = 0
        except Exception as e:
            elapsed = time.monotonic() - start
            consecutive_failures += 1
            logger.warning(
                "Ping failed sid=%s url=%s (%d/%d) cycle=%d elapsed=%.2fs error_type=%s error=%s",
                sid, url, consecutive_failures, max_consecutive_failures,
                cycle, elapsed,
                type(e).__name__, e,
            )
            if consecutive_failures >= max_consecutive_failures:
                raise
            # Sleep before retry so we don't tight-loop on a flapping gateway.
            await asyncio.sleep(sleep_time)
            continue
        elapsed = time.monotonic() - start
        logger.info("ping_ok sid=%s cycle=%d elapsed=%.3fs", sid, cycle, elapsed)
        if elapsed > _SLOW_PING_THRESHOLD:
            logger.warning(
                "Ping slow sid=%s elapsed=%.1fs (target=%ss)",
                sid, elapsed, sleep_time,
            )
        delay = max(0, sleep_time - elapsed)
        await asyncio.sleep(delay)


class _PingThread:
    """Dedicated thread + event loop for running ping tasks.

    Keeps pings isolated from the main event loop so they aren't starved
    when there are many concurrent sessions doing heavy I/O.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._client: aiohttp.ClientSession | None = None

    def _ensure_started(self) -> asyncio.AbstractEventLoop:
        if self._loop is not None and self._loop.is_running():
            return self._loop
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever, daemon=True, name="openreward-ping"
            )
            self._thread.start()
            return self._loop

    async def _get_client(self) -> aiohttp.ClientSession:
        if self._client is None or self._client.closed:
            self._client = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=0, force_close=True, enable_cleanup_closed=True),
                timeout=aiohttp.ClientTimeout(total=30),
                trace_configs=[_make_trace_config()],
            )
        return self._client

    async def _run_ping(self, url: str, sid: str, api_key: Optional[str], sleep_time: float, deployment: Optional[str]) -> None:
        client = await self._get_client()
        await ping(url, sid, api_key, sleep_time, client, deployment)

    def schedule(self, url: str, sid: str, api_key: Optional[str], sleep_time: float, deployment: Optional[str]) -> "concurrent.futures.Future[None]":
        loop = self._ensure_started()
        return asyncio.run_coroutine_threadsafe(
            self._run_ping(url, sid, api_key, sleep_time, deployment), loop
        )

    def _shutdown(self) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            return
        client = self._client

        async def _close():
            if client is not None and not client.closed:
                await client.close()

        try:
            asyncio.run_coroutine_threadsafe(_close(), loop).result(timeout=5)
        except Exception:
            pass
        loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5)


ping_thread = _PingThread()
atexit.register(ping_thread._shutdown)
