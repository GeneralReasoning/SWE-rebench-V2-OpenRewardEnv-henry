from __future__ import annotations

from typing import cast

from openai.types.responses import ResponseInputItemParam

from .models import NormalizedEvent, NormalizedType
from .utils import ensure_json_str, get


def serialize_openai_response(
    response: ResponseInputItemParam,
) -> NormalizedEvent:
    r_role = get(response, "role")
    r_type = get(response, "type", None)
    r_content = get(response, "content", None)

    if r_role in ("user", "system", "developer", "assistant"):
        content_joined = ""
        if isinstance(r_content, str):
            content_joined += r_content
        elif isinstance(r_content, list):
            for part in r_content:
                # Use get() to handle both dict and object types
                text = get(part, "text")
                refusal = get(part, "refusal")

                if text is not None:
                    content_joined += str(text)
                elif refusal is not None:
                    content_joined += str(refusal)
                else:
                    # Get part type for better error message
                    part_type = get(part, "type", "unknown")
                    raise ValueError(
                        f"Content part with type '{part_type}' is not supported. "
                        f"Expected 'text' or 'refusal' field. Part: {part}"
                    )
        
        old_to_new_rols: dict[str, NormalizedType] = {
            "developer": "system_message",
            "system": "system_message",
            "user": "user_message",
            "assistant": "assistant_message",
        }
        return NormalizedEvent(
            type=old_to_new_rols[r_role],
            content=content_joined,
        )
    elif r_type == "function_call":
        return NormalizedEvent(
            type="tool_call",
            name=get(response, "name"),
            content=ensure_json_str(get(response, "arguments")),
            call_id=get(response, "call_id"),
        )
    elif r_type == "function_call_output":
        return NormalizedEvent(
            type="tool_result",
            content=ensure_json_str(get(response, "output")),
            call_id=get(response, "call_id"),
        )
    elif r_type == "reasoning":
        content_joined = ""
        content_parts = get(response, "content", [])
        if content_parts:
            for part in get(response, "content", []):
                assert get(part, "type") == "reasoning_text"
                content_joined += f"{get(part, 'text')}\n"
        summary_joined = ""
        summary_parts = get(response, "summary", [])
        if summary_parts:
            for summary in get(response, "summary", []):
                assert get(summary, "type") == "summary_text"
                summary_joined += f"{get(summary, 'text')}\n"
        return NormalizedEvent(
            type="reasoning",
            content=content_joined.strip(),
            content_reference=f"reasoning:{get(response, 'id')}",
            summary=summary_joined.strip(),
        )
    else:
        # Check if this is a Response object (common mistake)
        if hasattr(response, 'output'):
            raise ValueError(
                "log_openai_response received a Response object. "
                "Please iterate over response.output items:\n"
                "  for item in response.output:\n"
                "    rollout.log_openai_response(item)"
            )

        # Unsupported type
        r_id = get(response, 'id', 'unknown')
        available_keys = list(response.keys()) if isinstance(response, dict) else [
            attr for attr in dir(response) if not attr.startswith('_')
        ]
        raise ValueError(
            f"Unsupported response item type. "
            f"Item id: {r_id}, type: {r_type}, role: {r_role}\n"
            f"Available fields: {available_keys}"
        )


if __name__ == "__main__":

    from openai.types.responses import (ResponseFunctionToolCall,
                                        ResponseReasoningItem)

    responses = [{'role': 'user',
    'content': "I'm at the Ferry Building in San Francisco. Use the tools to: (1) get current weather, and (2) suggest 2 good cafés nearby. Then give me a 1-paragraph plan."},
    ResponseReasoningItem(id='rs_0b77afeeab0315a8006910a0304c0081958276a1226f7d37e3', summary=[], type='reasoning', content=None, encrypted_content=None, status=None),
    ResponseFunctionToolCall(arguments='{"location":"San Francisco, CA","unit":"fahrenheit"}', call_id='call_Pk0zcuYHhASuXT896uzXrUWn', name='get_weather', type='function_call', id='fc_0b77afeeab0315a8006910a0378e8c8195bb23b3827865cdb7', status='completed'),
    ResponseFunctionToolCall(arguments='{"near":"Ferry Building, San Francisco, CA","limit":2}', call_id='call_EF14jcgLtWAZK61rBeGBTj8Y', name='find_cafes', type='function_call', id='fc_0b77afeeab0315a8006910a037d9908195a1de9ef375c87933', status='completed'),
    {'type': 'function_call_output',
    'call_id': 'call_Pk0zcuYHhASuXT896uzXrUWn',
    'output': '{"location": {"location": "San Francisco, CA", "unit": "fahrenheit"}, "temp": 18, "unit": "celsius", "condition": "Cloudy"}'},
    {'type': 'function_call_output',
    'call_id': 'call_EF14jcgLtWAZK61rBeGBTj8Y',
    'output': '{"near": {"near": "Ferry Building, San Francisco, CA", "limit": 2}, "results": ["Blue Bottle (66 Mint St)", "Sightglass (270 7th St)"]}'}]


    for response in responses:
        normalized = serialize_openai_response(cast(ResponseInputItemParam, response))
        print(normalized)
