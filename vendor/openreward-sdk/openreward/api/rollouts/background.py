from __future__ import annotations

import asyncio
import json
import random
import threading

import time
from collections import deque
from concurrent.futures import Future
from dataclasses import asdict
from datetime import datetime
from typing import Deque, Optional, Union

from aiohttp import ClientResponseError, ClientSession

from openreward._version import USER_AGENT
from openreward.http_client import make_request
from openreward.models import (FlushEvent, InputEvent, LogMessageEvent,
                               RolloutStartedEvent, RolloutUpdateEvent,
                               SendLoopConfig, ShutdownEvent)

from openreward.log_utils import get_logger as _get_logger
log = _get_logger("openreward.api.rollouts")

# Special background logic
def _strip_none(d: dict) -> dict:
    """Recursively remove keys with None values from a dict."""
    return {
        k: _strip_none(v) if isinstance(v, dict) else v
        for k, v in d.items()
        if v is not None
    }

def _encode_event(e: Union[LogMessageEvent, RolloutStartedEvent, RolloutUpdateEvent]) -> bytes:
    """Transform client events to server-compatible format."""
    data = asdict(e)
    filtered_data = _strip_none(data)
    return json.dumps(filtered_data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def _jittered_deadline(config: SendLoopConfig) -> float:
    return config.max_age * (1.0 + random.uniform(-config.jitter, config.jitter))

async def _async_worker_loop(in_queue: asyncio.Queue, config: SendLoopConfig):
    # log.debug("worker loop started", base_url=config.base_url, ring_capacity=config.ring_capacity, max_items=config.max_items, max_bytes=config.max_bytes, max_age=config.max_age)

    session = ClientSession(base_url=config.base_url)

    ring_buffer: Deque[bytes] = deque(maxlen=config.ring_capacity)  # ring buffer (stores JSON-encoded items)
    total_buffered_bytes = 0
    first_enqueue_time: Optional[float] = None

    upload_queue: "asyncio.Queue[dict | None]" = asyncio.Queue()
    flush_timer_task: Optional[asyncio.Task] = None
    input_get_task: asyncio.Task = asyncio.create_task(in_queue.get())

    async def upload_worker():
        pending: set[asyncio.Task] = set()

        async def do_upload(payload: dict):
            n_events = len(payload.get("events", []))
            # log.debug("uploading batch", n_events=n_events)
            t0 = time.perf_counter()
            await make_request(
                client=session,
                url="/v1/rollouts",
                method="POST",
                data={},
                headers={"x-api-key": config.api_key, "User-Agent": USER_AGENT},
                body=payload,
                max_retries=config.max_retries,
                backoff_base=config.backoff_base,
                backoff_factor=config.backoff_factor,
                backoff_cap=config.backoff_cap,
            )
            elapsed = time.perf_counter() - t0
            # log.debug("batch uploaded", n_events=n_events, elapsed_s=round(elapsed, 3))
            if elapsed >= 1.0:
                log.debug("slow upload", elapsed_s=round(elapsed, 3))

        while True:
            while len(pending) >= config.max_upload_concurrency:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    if exc := task.exception():
                        # TODO: remove when rollout upload 500s are fixed server-side
                        if isinstance(exc, ClientResponseError) and exc.status == 500:
                            log.debug("upload failed", error=str(exc))
                        else:
                            log.error("upload failed", error=str(exc))

            payload = await upload_queue.get()
            if payload is None:
                upload_queue.task_done()
                break

            pending.add(asyncio.create_task(do_upload(payload)))
            upload_queue.task_done()

        if pending:
            done, _ = await asyncio.wait(pending)
            for task in done:
                if exc := task.exception():
                    # TODO: remove when rollout upload 500s are fixed server-side
                    if isinstance(exc, ClientResponseError) and exc.status == 500:
                        log.debug("upload failed", error=str(exc))
                    else:
                        log.error("upload failed", error=str(exc))

    uploader_task = asyncio.create_task(upload_worker())

    def arm_flush_timer():
        nonlocal flush_timer_task
        if flush_timer_task is None and ring_buffer:
            flush_timer_task = asyncio.create_task(asyncio.sleep(_jittered_deadline(config)))

    def cancel_flush_timer():
        nonlocal flush_timer_task
        if flush_timer_task and not flush_timer_task.done():
            flush_timer_task.cancel()
        flush_timer_task = None

    def should_flush() -> bool:
        if len(ring_buffer) >= config.max_items: return True
        if total_buffered_bytes >= config.max_bytes: return True
        if first_enqueue_time and (time.time() - first_enqueue_time) >= config.max_age: return True
        return False

    def drain_one_batch_bytes() -> Optional[dict]:
        """Pop up to limits from the ring. Return dict with events array or None if empty."""
        nonlocal total_buffered_bytes, first_enqueue_time
        if not ring_buffer:
            return None

        items: list[bytes] = []
        size: int = 0

        while ring_buffer and len(items) < config.max_batch_items:
            p = ring_buffer[0]  # peek without popping
            if items and size + len(p) > config.max_batch_bytes:
                break  # adding this item would exceed the limit
            ring_buffer.popleft()
            items.append(p)
            size += len(p)
            total_buffered_bytes -= len(p)

        if not ring_buffer:  # emptied the ring
            first_enqueue_time = None

        # Parse the JSON items and wrap in events object
        events = [json.loads(item) for item in items]
        return {"events": events}

    def enqueue_batches_until_below_thresholds(full_flush: bool):
        """full_flush=True drains everything; else drain until under thresholds."""
        nonlocal first_enqueue_time
        while ring_buffer:
            if not full_flush and not should_flush():
                break
            batch_bytes = drain_one_batch_bytes()
            if batch_bytes:
                upload_queue.put_nowait(batch_bytes)
            else:
                break
        # re-arm or cancel the timer based on remaining buffer
        if ring_buffer and first_enqueue_time is not None:
            arm_flush_timer()
        else:
            cancel_flush_timer()

    try:
        while True:
            awaitables = [input_get_task]
            if flush_timer_task:
                awaitables.append(flush_timer_task)

            completed, _pending = await asyncio.wait(awaitables, return_when=asyncio.FIRST_COMPLETED)

            # 1) timer fired -> age flush
            if flush_timer_task and flush_timer_task in completed:
                flush_timer_task = None
                # log.debug("flush timer fired", buffered_items=len(ring_buffer), buffered_bytes=total_buffered_bytes)
                enqueue_batches_until_below_thresholds(full_flush=False)

            # 2) input arrived
            if input_get_task in completed:
                event = input_get_task.result()

                if isinstance(event, ShutdownEvent):
                    # log.debug("shutdown received, flushing and stopping", buffered_items=len(ring_buffer), buffered_bytes=total_buffered_bytes)
                    # flush everything, stop accepting, finish uploads
                    enqueue_batches_until_below_thresholds(full_flush=True)
                    await upload_queue.join()
                    await upload_queue.put(None) # stop uploader
                    await uploader_task
                    # log.debug("worker loop stopped")
                    return

                elif isinstance(event, FlushEvent):
                    # log.debug("flush received", buffered_items=len(ring_buffer), buffered_bytes=total_buffered_bytes)
                    enqueue_batches_until_below_thresholds(full_flush=True)

                elif isinstance(event, (LogMessageEvent, RolloutStartedEvent, RolloutUpdateEvent)):
                    encoded_event = _encode_event(event)
                    # If ring is full, deque will drop oldest automatically; account for bytes
                    if len(ring_buffer) == ring_buffer.maxlen and ring_buffer.maxlen is not None:
                        # we are about to overwrite the oldest — simulate drop by popleft first
                        evicted_oldest = ring_buffer.popleft()
                        total_buffered_bytes -= len(evicted_oldest)
                        log.warning("ring buffer full, evicted oldest event", ring_capacity=ring_buffer.maxlen)
                        # keep first_enqueue_time pointing at the true oldest; will be reset below if ring empties
                    # append new entry
                    ring_buffer.append(encoded_event)
                    total_buffered_bytes += len(encoded_event)
                    # log.debug("event buffered", event_type=type(event).__name__, buffered_items=len(ring_buffer), buffered_bytes=total_buffered_bytes)
                    if first_enqueue_time is None:
                        first_enqueue_time = time.time()
                        arm_flush_timer()

                    # maybe flush due to count/byte trigger
                    enqueue_batches_until_below_thresholds(full_flush=False)

                else:
                    log.warning("unknown event type, ignoring", event_type=type(event).__name__)

                # Prime next receive for non-shutdown events
                input_get_task = asyncio.create_task(in_queue.get())
    finally:
        try:
            cancel_flush_timer()
            if not uploader_task.done():
                await upload_queue.put(None)
                await uploader_task
            await session.close()
        except Exception:
            pass


async def _send_rollout_start(config: SendLoopConfig, event: RolloutStartedEvent) -> None:
    """Send a RolloutStartedEvent synchronously (with retries) before any messages are queued."""
    from aiohttp import ClientSession
    from openreward._version import USER_AGENT
    data = _strip_none(asdict(event))
    async with ClientSession(base_url=config.base_url) as session:
        await make_request(
            client=session,
            url="/v1/rollouts",
            method="POST",
            data={},
            headers={"x-api-key": config.api_key, "User-Agent": USER_AGENT},
            body={"events": [data]},
            max_retries=config.max_retries,
            backoff_base=config.backoff_base,
            backoff_factor=config.backoff_factor,
            backoff_cap=config.backoff_cap,
        )


def start_background_worker(config: SendLoopConfig) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue, threading.Thread]:
    """Start a background thread with its own event loop running the worker."""
    # log.debug("starting background worker thread")
    loop = asyncio.new_event_loop()
    queue_future: Future[asyncio.Queue] = Future()

    def run():
        asyncio.set_event_loop(loop)
        in_queue: asyncio.Queue = asyncio.Queue()
        queue_future.set_result(in_queue)
        loop.run_until_complete(_async_worker_loop(in_queue, config))
        loop.close()

    thread = threading.Thread(target=run, name="openreward_rollout_worker", daemon=True)
    thread.start()

    # Wait until the queue has been created on the worker loop
    in_queue = queue_future.result(timeout=10)
    # log.debug("background worker thread started", thread=thread.name)
    return loop, in_queue, thread
