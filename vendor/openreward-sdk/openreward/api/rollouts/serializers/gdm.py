from typing import List

try:
    from typing import NotRequired
except ImportError:
    from typing_extensions import NotRequired

import json

from google.genai import types as gdm_types

from .models import NormalizedEvent, NormalizedType
from .utils import get


def serialize_gdm_message(message: gdm_types.Content) -> List[NormalizedEvent]:
    events: List[NormalizedEvent] = []

    role_to_type: dict[str, NormalizedType] = {
        "user": "user_message",
        "assistant": "assistant_message",
    }

    role = get(message, "role")
    if role == "model":
        assert message.parts is not None
        for part in message.parts:
            if part.text:
                if part.thought:
                    events.append(NormalizedEvent(
                        type="reasoning",
                        content=str(part.text),
                        content_reference=str(part.thought_signature),
                    ))
                else:
                    ev_type = role_to_type.get(role)
                    if ev_type is not None:
                        events.append(NormalizedEvent(
                            type=ev_type,
                            content=str(part.text),
                        ))
            elif part.function_call:
                name = part.function_call.name
                args = part.function_call.args
                args_str = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
                events.append(NormalizedEvent(
                    type="tool_call",
                    name=name,
                    content=args_str,
                    call_id=part.function_call.id,
                ))
    elif role == "user":
        assert message.parts is not None
        for part in message.parts:
            if part.text:
                ev_type = role_to_type.get(role)
                if ev_type is not None:
                    events.append(NormalizedEvent(
                        type=ev_type,
                        content=part.text,
                    ))
            elif part.function_response:
                assert part.function_response.response is not None
                tool_id = part.function_response.id
                tool_result = part.function_response.response["result"]
                events.append(NormalizedEvent(
                    type="tool_result",
                    content=tool_result,
                    call_id=tool_id,
                ))
    else:
        raise ValueError(f"Unknown role: {role}")

    return events
