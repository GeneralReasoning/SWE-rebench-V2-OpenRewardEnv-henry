from dataclasses import dataclass
from typing import Any, Dict, Literal, Optional, Union

from pydantic import BaseModel

from openreward.api.rollouts.serializers.models import NormalizedType


TrainingStage = Literal["pretrained", "sft", "rl"]
RunType = Literal["train", "eval", "adhoc"]


@dataclass
class RunInfo:
    """Run-level context — constant for every rollout in a training run."""
    model_name: str
    run_type: Optional[RunType] = None
    model_params: Optional[int] = None
    model_active_params: Optional[int] = None
    training_stage: Optional[TrainingStage] = None
    initial_step: Optional[int] = None
    checkpoint: Optional[str] = None
    peft_type: Optional[str] = None
    peft_rank: Optional[int] = None
    rl_algorithm: Optional[str] = None
    batch_size: Optional[int] = None
    lr: Optional[float] = None
    optimizer: Optional[str] = None
    framework: Optional[str] = None
    framework_version: Optional[str] = None


@dataclass
class RolloutInfo:
    task_index: int
    env_version: Optional[str] = None
    current_step: Optional[int] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    max_tokens: Optional[int] = None
    num_compactions: Optional[int] = None
    duration_ms: Optional[int] = None
    harness: Optional[str] = None


class RolloutConfig(BaseModel):
    run_name: str
    rollout_name: Optional[str] = None
    environment: Optional[str] = None
    variant: Optional[str] = None
    split: Optional[str] = None
    metadata: Optional[dict] = None
    task_spec: Optional[Dict[str, Any]] = None
    run_info: Optional[dict] = None


@dataclass
class Config:
    shutdown_timeout: float
    send_loop_config: "SendLoopConfig"


@dataclass
class SendLoopConfig:
    max_items: int # max item threshold
    max_bytes: int # max byte threshold
    max_age: float # max age
    jitter: float # % jitter of the flush interval

    ring_capacity: int # max items, at which the ring buffer will be flushed

    max_batch_items: int # max number of items to flush at once
    max_batch_bytes: int # max number of bytes to flush at once

    max_retries: int # max number of retries
    backoff_base: float # base time of the backoff
    backoff_factor: float # factor of the backoff
    backoff_cap: float # cap time of the backoff

    max_upload_concurrency: int

    api_key: str
    base_url: str

@dataclass
class LogMessageEvent:
    # rollout info
    eventId: str
    timestamp: int
    index: int

    rolloutEventId: str

    # normalized event info
    type: NormalizedType
    content: Optional[str] = None # visible text or JSON string for tools
    contentReference: Optional[str] = None # only for hidden reasoning
    summary: Optional[str] = None # reasoning-only
    name: Optional[str] = None # tool name (tool_call only)
    callId: Optional[str] = None # join key for tool_call/result

    # extra info
    environment_id: Optional[str] = None
    reward: Optional[float] = None
    isFinished: Optional[bool] = None
    metadata: Optional[Dict[str, Any]] = None

    eventType: Literal["message"] = "message"


@dataclass # Do we want this to be on the background process?
class RolloutStartedEvent:
    # rollout info
    eventId: str
    timestamp: int

    # rollout info
    runName: str
    step: Optional[int] = None
    environment: Optional[str] = None
    environment_id: Optional[str] = None
    rolloutName: Optional[str] = None
    variant: Optional[str] = None
    variant_id: Optional[str] = None
    split: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    task_spec: Optional[Dict[str, Any]] = None
    run_info: Optional[Dict[str, Any]] = None
    rollout_info: Optional[Dict[str, Any]] = None

    eventType: Literal["rollout"] = "rollout"

    @classmethod
    def from_config(cls, event_id: str, timestamp: int, config: RolloutConfig, step: Optional[int] = None, environment_id: Optional[str] = None, variant_id: Optional[str] = None):
        return cls(
            eventId=event_id,
            timestamp=timestamp,
            runName=config.run_name,
            rolloutName=config.rollout_name,
            environment=config.environment,
            environment_id=environment_id,
            variant=config.variant,
            variant_id=variant_id,
            split=config.split,
            metadata=config.metadata,
            task_spec=config.task_spec,
            run_info=config.run_info,
            step=step
        )

@dataclass
class RolloutUpdateEvent:
    """Mid-rollout metadata update (e.g. rollout_info at end of rollout)."""
    eventId: str          # the rollout's eventId
    timestamp: int
    rollout_info: Optional[Dict[str, Any]] = None
    eventType: Literal["rollout_update"] = "rollout_update"

@dataclass
class FlushEvent:
    event_type: Literal["flush"] = "flush"

@dataclass
class ShutdownEvent:
    event_type: Literal["shutdown"] = "shutdown"

InputEvent = Union[LogMessageEvent, RolloutStartedEvent, RolloutUpdateEvent, FlushEvent, ShutdownEvent]

