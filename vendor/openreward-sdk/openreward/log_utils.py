import logging
import os
import structlog
import sys

from openreward._version import __version__ as _sdk_version


OPENREWARD_USE_STRUCTURED_LOGS = bool(os.getenv("OPENREWARD_USE_STRUCTURED_LOGS", False))

# Set by the provisioner when running in a managed env-server pod. Tags every
# structured log line so a log can be traced back to the exact build.
_openreward_build_sha = os.getenv("OPENREWARD_BUILD_SHA")


def _add_runtime_metadata(_, __, event_dict):
    event_dict["sdk_version"] = _sdk_version
    if _openreward_build_sha:
        event_dict["build_sha"] = _openreward_build_sha
    return event_dict


_SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]

_STRUCTURED_PROCESSORS = [*_SHARED_PROCESSORS, _add_runtime_metadata]

def _rename_for_gcp(_, method, event_dict):
    event_dict["message"] = event_dict.pop("event")
    event_dict["severity"] = event_dict.pop("level", method).upper()
    return event_dict


def _resolve_log_level() -> int:
    """Resolve log level from env vars: OPENREWARD_LOG_LEVEL -> LOG_LEVEL -> INFO."""
    raw = os.environ.get("OPENREWARD_LOG_LEVEL") or os.environ.get("LOG_LEVEL") or "INFO"
    return getattr(logging, raw.upper(), logging.INFO)


def get_logger(name: str) -> structlog.BoundLogger:
    """Return a structlog logger scoped to openreward with instance-level config.

    This avoids polluting the global ``structlog.configure()`` namespace so that
    training scripts importing the SDK don't see debug spam when the environment
    server's ``setup_logging()`` has not been called.
    """
    if OPENREWARD_USE_STRUCTURED_LOGS:
        processors = [*_STRUCTURED_PROCESSORS, _rename_for_gcp, structlog.processors.JSONRenderer()]
    else:
        processors = [*_SHARED_PROCESSORS, structlog.dev.ConsoleRenderer()]

    return structlog.wrap_logger(
        structlog.PrintLogger(),
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_log_level()),
    ).bind(logger_name=name)


def setup_logging(level: int = logging.INFO):
    """Configure logging for the current process.

    Uses JSON structured logging when OPENREWARD_USE_STRUCTURED_LOGS is set,
    otherwise uses a human-readable console renderer.
    """
    if OPENREWARD_USE_STRUCTURED_LOGS:
        final_processors = [*_STRUCTURED_PROCESSORS, _rename_for_gcp, structlog.processors.JSONRenderer()]
    else:
        final_processors = [*_SHARED_PROCESSORS, structlog.dev.ConsoleRenderer()]

    structlog.configure(
        processors=final_processors,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    if OPENREWARD_USE_STRUCTURED_LOGS:
        # Production: also configure stdlib root logger so that third-party
        # library messages (uvicorn, aiohttp, etc.) flow through structlog.
        formatter = structlog.stdlib.ProcessorFormatter(
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(),
            ],
            foreign_pre_chain=[*_STRUCTURED_PROCESSORS, structlog.stdlib.ExtraAdder()],
        )

        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        root = logging.getLogger()
        root.handlers.clear()
        root.addHandler(handler)
        root.setLevel(level)
