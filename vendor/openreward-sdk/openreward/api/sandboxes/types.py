from typing import Literal, Optional, Union

from pydantic import BaseModel, field_validator, model_validator

# Machine size in format 'cpu:memory' (e.g., '1:2' = 1 CPU, 2GB memory)
MachineSize = Literal[
    '0.5:0.5',
    '1:1',
    '2:2',
    '4:4',
    '0.5:1',
    '1:2',
    '2:4',
    '4:8',
    '0.5:2',
    '1:4',
    '2:8',
    '4:16',
    'nvidia-l4'
]

class SandboxBucketConfig(BaseModel):
    mount_path: str
    """Path inside the container where the bucket will be mounted."""

    read_only: Literal[True] = True
    """Buckets are always mounted in read-only mode."""

    only_dir: Optional[str] = None
    """If set, only mount the specified directory from the bucket."""

    implicit_dirs: bool = True
    """If True, mount all subdirectories of the bucket."""

class SandboxSidecarContainer(BaseModel):
    name: str
    """Unique name for this sidecar container."""

    image: str
    """Container image to run."""

    command: Optional[list[str]] = None
    """Optional command override for the container entrypoint."""

    args: Optional[list[str]] = None
    """Optional arguments to pass to the command."""

    env: Optional[dict[str, str]] = None
    """Environment variables for this container."""

    ports: Optional[list[int]] = None
    """Ports this container exposes (informational, used for probes)."""

    @field_validator("name")
    def validate_name_not_reserved(cls, value: str) -> str:
        if value.lower() in {"main", "sidecar"}:
            raise ValueError("Sidecar container names 'main' and 'sidecar' are reserved.")
        return value


class SandboxHostAlias(BaseModel):
    """Configuration for adding hostname aliases to /etc/hosts."""

    ip: str
    """IP address to map hostnames to, e.g. '127.0.0.1'."""

    hostnames: list[str]
    """List of hostnames to map to the IP"""

# HACK: Hardcoded list of common API key env var names. We send both upper and
# lowercase versions to prevent a common failure mode where the server-side
# code expects one casing but the user provides the other.
_API_KEY_NAMES = frozenset({
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "TAVILY_API_KEY",
    "GOOGLE_API_KEY",
    "COHERE_API_KEY",
    "MISTRAL_API_KEY",
    "GROQ_API_KEY",
    "TOGETHER_API_KEY",
    "REPLICATE_API_TOKEN",
    "HUGGINGFACE_API_KEY",
    "HF_TOKEN",
    "PERPLEXITY_API_KEY",
    "FIREWORKS_API_KEY",
    "DEEPSEEK_API_KEY",
    "KAGGLE_KEY",
    "KAGGLE_USERNAME",
    "E2B_API_KEY",
    "MODAL_TOKEN_ID",
    "MODAL_TOKEN_SECRET",
    "DAYTONA_API_KEY",
})

class SandboxSettings(BaseModel):
    """Compute resource settings for a container."""

    environment: str
    """OpenReward environment to run the container in."""

    image: str
    """Container image to run, e.g. "python:3.10-slim"."""

    machine_size: MachineSize
    """
    Machine size to run the container in.
    Example: "0.5:1" = 0.5 CPU and 1 GB memory.
    """

    # TODO: do we want these?
    # disk_request: Optional[str] = None
    # """Minimum ephemeral storage requested (if using emptyDir volumes). Example: "1Gi" = 1 GB."""

    # disk_limit: Optional[str] = None
    # """Maximum ephemeral storage allowed; exceeding this causes eviction."""

    env: Optional[dict[str, str]] = None
    """Environment variables for the container, e.g. {"ENV": "prod"}."""

    block_network: bool = False
    """If True, disables outbound network access (Kubernetes egress policies required)."""

    bucket_config: Optional[SandboxBucketConfig] = None
    """List of buckets to mount; mounts them into the container at runtime."""

    # labels: Optional[dict[str, str]] = None
    # """Labels to add to running computer for attribution."""

    sidecars: Optional[list[SandboxSidecarContainer]] = None
    """Additional containers to run in the same pod."""

    host_aliases: Optional[list[SandboxHostAlias]] = None
    """Hostname aliases to add to /etc/hosts in all containers."""

    @model_validator(mode="after")
    def _duplicate_api_keys_lowercase(self) -> "SandboxSettings":
        """HACK: duplicate well-known API key env vars in lowercase to prevent
        common failure point with naming inconsistency between client and server."""
        if self.env is None:
            return self
        for key in _API_KEY_NAMES:
            if key in self.env:
                lower = key.lower()
                if lower not in self.env:
                    self.env[lower] = self.env[key]
        return self

class RunResult:
    """Result of a sandbox.run() call.

    Supports backwards-compatible 2-tuple unpacking::

        output, return_code = sandbox.run("ls")

    New fields are accessible as attributes::

        result = sandbox.run("ls")
        if result.truncated:
            print("Output was cut short!")
        if result.timed_out:
            print("Command exceeded timeout!")
    """

    def __init__(
        self,
        output: str,
        return_code: int,
        truncated: bool = False,
        timed_out: bool = False,
        sanitised: bool = True,
    ) -> None:
        self.output = output
        self.return_code = return_code
        self.truncated = truncated
        self.timed_out = timed_out
        self.sanitised = sanitised

    def __iter__(self):
        """Yield (output, return_code) for backwards-compatible 2-tuple unpacking."""
        yield self.output
        yield self.return_code

    def __repr__(self) -> str:
        return (
            f"RunResult(return_code={self.return_code}, "
            f"truncated={self.truncated}, timed_out={self.timed_out}, "
            f"output={self.output[:50]!r}{'...' if len(self.output) > 50 else ''})"
        )


class PodTerminatedError(RuntimeError):
    def __init__(self, reason: str, *, sid: Optional[str]):
        super().__init__(f"Pod terminated (sid={sid!r}): {reason}")
        self.reason = reason
        self.sid = sid
