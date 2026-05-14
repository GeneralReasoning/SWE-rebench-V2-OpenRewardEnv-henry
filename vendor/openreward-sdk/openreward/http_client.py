import asyncio
from typing import Any, Dict, Optional

from aiohttp import (ClientError, ClientResponseError, ClientSession,
                     ContentTypeError)
from tenacity import (AsyncRetrying, retry_if_exception, stop_after_attempt,
                      wait_exponential)


def should_retry_on_500(exception: BaseException) -> bool:
    """Retry only on 5xx server errors"""
    if isinstance(exception, ClientResponseError):
        return 500 <= exception.status < 600
    return False


async def make_request(
    client: ClientSession,
    url: str,
    method: str,
    data: Dict[str, Any],
    headers: Dict[str, str],
    body: Optional[Dict[str, Any]],
    max_retries: int,
    backoff_base: float,
    backoff_factor: float,
    backoff_cap: float,
) -> Any:

    retryer = AsyncRetrying(
        retry=retry_if_exception(should_retry_on_500),
        wait=wait_exponential(multiplier=backoff_base, exp_base=backoff_factor, max=backoff_cap),
        stop=stop_after_attempt(1 + max_retries),
        reraise=True,
    )

    async for attempt in retryer:
        with attempt:
            async with client.request(
                method.upper(),
                url,
                params=data,
                json=body,
                headers=headers,
            ) as resp:
                text = await resp.text()
                try:
                    resp.raise_for_status()
                except ClientResponseError as e:
                    raise ClientResponseError(
                        request_info=e.request_info,
                        history=e.history,
                        status=e.status,
                        message=text,
                        headers=e.headers,
                    )
                try:
                    return await resp.json()
                except (ContentTypeError, ValueError):
                    text = await resp.text()
                    return {"status": resp.status, "text": text}

    raise RuntimeError("Retry exhausted without return or exception")
