import json
from typing import Any, List, Optional, Union

from .models import NormalizedEvent, NormalizedType


def get(obj: Any, key: str, default: Any = None) -> Any:
    """Duck-typed attribute/dict accessor."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def add_text_event(events: List[NormalizedEvent], role: str, text: Optional[str]) -> None:
    text = (text or "").strip()
    if not text:
        return

    role_to_type: dict[str, NormalizedType] = {
        "user": "user_message",
        "assistant": "assistant_message",
        "system": "system_message",
    }
    ev_type = role_to_type.get(role)
    if not ev_type:
        return  # Unknown role; ignore

    events.append(NormalizedEvent(type=ev_type, content=text))

def ensure_json_str(x: Any) -> str:
    """Return a JSON string; pass strings through, dump dicts/lists/objects."""
    if isinstance(x, str):
        return x
    return json.dumps(x, ensure_ascii=False)


def stringify_if_needed(x: Any) -> Optional[str]:
    """Return None, the string as-is, or a JSON string for non-strings."""
    if x is None:
        return None
    return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)


