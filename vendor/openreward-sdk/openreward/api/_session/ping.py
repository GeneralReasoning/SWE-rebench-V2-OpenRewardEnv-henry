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


async def ping(url: str, sid: str, api_key: Optional[str], sleep_time: float, client: aiohttp.ClientSession, deployment: Optional[str] = None, max_consecutive_failures: int = _MAX_CONSECUTIVE_PING_FAILURES) -> None:
    """Keepalive ping loop. Successful pings reset the consecutive-failure
    counter; only raise after ``max_consecutive_failures`` back-to-back
    failures with no intervening success.
    """
    consecutive_failures = 0
    while True:
        start = time.monotonic()
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
            consecutive_failures += 1
            logger.warning(
                "Ping failed sid=%s url=%s (%d/%d) error_type=%s error=%s",
                sid, url, consecutive_failures, max_consecutive_failures,
                type(e).__name__, e,
            )
            if consecutive_failures >= max_consecutive_failures:
                raise
            # Sleep before retry so we don't tight-loop on a flapping gateway.
            await asyncio.sleep(sleep_time)
            continue
        elapsed = time.monotonic() - start
        logger.debug("Ping ok sid=%s elapsed=%.2fs", sid, elapsed)
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
