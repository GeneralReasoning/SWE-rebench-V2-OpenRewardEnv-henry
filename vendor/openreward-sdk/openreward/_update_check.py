import json
import os
import re
import threading
import urllib.request
from typing import Optional, Tuple

from openreward._version import __version__
from openreward.log_utils import get_logger

_PYPI_URL = "https://pypi.org/pypi/openreward/json"
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_DISABLE_ENV_VAR = "OPENREWARD_DISABLE_UPDATE_CHECK"

_checked = False
_checked_lock = threading.Lock()

logger = get_logger("openreward._update_check")


def _parse_version(v: str) -> Optional[Tuple[int, int, int]]:
    m = _VERSION_RE.match(v)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _fetch_latest_version(timeout: float) -> Optional[str]:
    try:
        req = urllib.request.Request(_PYPI_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return data.get("info", {}).get("version")
    except Exception:
        return None


def _run_check(timeout: float) -> None:
    try:
        current = _parse_version(__version__)
        if current is None:
            return

        latest_str = _fetch_latest_version(timeout)
        if latest_str is None:
            return
        latest = _parse_version(latest_str)
        if latest is None:
            return

        # Only warn when at least a minor version behind — ignore patch-level drift.
        major_behind = latest[0] > current[0]
        minor_behind = latest[0] == current[0] and latest[1] > current[1]
        if major_behind or minor_behind:
            logger.warning(
                "sdk_version_outdated",
                current_version=__version__,
                latest_version=latest_str,
                message=(
                    f"openreward {__version__} is out of date (latest is {latest_str}). "
                    f"Upgrade with `pip install -U openreward`."
                ),
            )
    except Exception:
        return


def check_for_updates_async(timeout: float = 2.0) -> None:
    """Kick off a best-effort PyPI version check in a daemon thread.

    Runs at most once per process. Fails silently on network or parsing errors.
    Set OPENREWARD_DISABLE_UPDATE_CHECK=1 to disable.
    """
    global _checked
    if os.getenv(_DISABLE_ENV_VAR):
        return
    with _checked_lock:
        if _checked:
            return
        _checked = True

    thread = threading.Thread(
        target=_run_check,
        args=(timeout,),
        name="openreward-update-check",
        daemon=True,
    )
    thread.start()
