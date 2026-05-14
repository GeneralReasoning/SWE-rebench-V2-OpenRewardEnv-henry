import os
from typing import Mapping, Optional, Union
from urllib.parse import urlparse, urlunparse

from openreward._update_check import check_for_updates_async
from openreward.models import Config, RunInfo, SendLoopConfig
from openreward.api.rollouts.rollout import RolloutAPI
from openreward.api.environments.client import EnvironmentsAPI, AsyncEnvironmentsAPI
from openreward.api.sandboxes.client import SandboxesAPI, AsyncSandboxesAPI
from openreward.api.sandboxes.types import SandboxSettings

OPENREWARD_API_KEY_ENV_VAR_NAME = "OPENREWARD_API_KEY"
DEFAULT_BASE_URL = "https://openreward.ai"


def _prepend_subdomain(url: str, subdomain: str) -> str:
    parsed = urlparse(url)
    new_netloc = f"{subdomain}.{parsed.netloc}"
    return urlunparse(parsed._replace(netloc=new_netloc))


class _BaseOpenReward:
    """Shared initialization logic for sync and async clients."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, run_info: Optional[RunInfo] = None):
        check_for_updates_async()
        self.api_key = api_key or os.getenv(OPENREWARD_API_KEY_ENV_VAR_NAME, "")
        assert self.api_key is not None

        base_url = base_url or os.getenv("OPENREWARD_URL", DEFAULT_BASE_URL)
        assert base_url is not None

        self._web_base_url = base_url
        self.base_url = os.getenv("OPENREWARD_API_URL") or _prepend_subdomain(base_url, "api")
        self.session_base_url = os.getenv("OPENREWARD_SESSION_URL") or _prepend_subdomain(base_url, "sessions")
        self._run_info = run_info
        self._rollout_api: Optional[RolloutAPI] = None

        self.config = Config(
            shutdown_timeout=10.0,
            send_loop_config=SendLoopConfig(
                max_items=128,
                max_bytes=4_000_000,
                max_age=1.0,
                jitter=0.05,
                ring_capacity=100_000,
                max_batch_items=128,
                max_batch_bytes=1_000_000,
                max_retries=4,
                backoff_base=0.5,
                backoff_factor=2.0,
                backoff_cap=30.0,
                max_upload_concurrency=32,
                api_key=self.api_key,
                base_url=self.base_url,
            ),
        )


    @property
    def run_info(self) -> Optional[RunInfo]:
        return self._run_info

    @run_info.setter
    def run_info(self, value: Optional[RunInfo]) -> None:
        self._run_info = value
        if self._rollout_api is not None:
            self._rollout_api.run_info = value


class AsyncOpenReward(_BaseOpenReward):
    """Async client for the OpenReward API."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, run_info: Optional[RunInfo] = None):
        super().__init__(api_key, base_url, run_info)
        self._environments_api: Optional[AsyncEnvironmentsAPI] = None

    @property
    def rollout(self) -> RolloutAPI:
        if self._rollout_api is None:
            self._rollout_api = RolloutAPI(
                send_loop_config=self.config.send_loop_config,
                shutdown_timeout=self.config.shutdown_timeout,
                web_base_url=self._web_base_url,
                run_info=self._run_info,
            )
        return self._rollout_api

    @property
    def environments(self) -> AsyncEnvironmentsAPI:
        if self._environments_api is None:
            self._environments_api = AsyncEnvironmentsAPI(
                base_url=self.session_base_url,
                api_key=self.api_key,
                api_base_url=self.base_url,
            )
        return self._environments_api

    def sandbox(
        self,
        settings: SandboxSettings,
        creation_timeout: int = 60 * 30,
        secrets: Optional[Mapping[str, Union[str, tuple[str, list[str]]]]] = None,
    ) -> AsyncSandboxesAPI:
        if self.api_key is None:
            raise ValueError("API key is required for sandbox API")
        return AsyncSandboxesAPI(
            base_url=self.session_base_url,
            api_key=self.api_key,
            settings=settings,
            creation_timeout=creation_timeout,
            secrets=secrets,
            api_base_url=self.base_url,
        )


class OpenReward(_BaseOpenReward):
    """Sync client for the OpenReward API."""

    def __init__(self, api_key: Optional[str] = None, base_url: Optional[str] = None, run_info: Optional[RunInfo] = None):
        super().__init__(api_key, base_url, run_info)
        self._environments_api: Optional[EnvironmentsAPI] = None

    @property
    def rollout(self) -> RolloutAPI:
        if self._rollout_api is None:
            self._rollout_api = RolloutAPI(
                send_loop_config=self.config.send_loop_config,
                shutdown_timeout=self.config.shutdown_timeout,
                web_base_url=self._web_base_url,
                run_info=self._run_info,
            )
        return self._rollout_api

    @property
    def environments(self) -> EnvironmentsAPI:
        if self._environments_api is None:
            self._environments_api = EnvironmentsAPI(
                base_url=self.session_base_url,
                api_key=self.api_key,
                api_base_url=self.base_url,
            )
        return self._environments_api

    def sandbox(
        self,
        settings: SandboxSettings,
        creation_timeout: int = 60 * 30,
        secrets: Optional[Mapping[str, Union[str, tuple[str, list[str]]]]] = None,
    ) -> SandboxesAPI:
        if self.api_key is None:
            raise ValueError("API key is required for sandbox API")
        return SandboxesAPI(
            base_url=self.session_base_url,
            api_key=self.api_key,
            settings=settings,
            creation_timeout=creation_timeout,
            secrets=secrets,
            api_base_url=self.base_url,
        )

    def close(self):
        """Clean up resources."""
        if self._environments_api is not None:
            self._environments_api.close()

    def __enter__(self) -> "OpenReward":
        return self

    def __exit__(self, *exc):
        self.close()
