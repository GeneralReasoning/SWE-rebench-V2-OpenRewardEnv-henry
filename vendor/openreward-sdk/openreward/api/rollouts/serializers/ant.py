from typing import List

try:
    from typing import NotRequired
except ImportError:
    from typing_extensions import NotRequired

import json

from anthropic.types import MessageParam as AnthropicMessageParam

from .models import NormalizedEvent, NormalizedType
from .utils import get


def serialize_anthropic_message(message: AnthropicMessageParam) -> List[NormalizedEvent]:
    events: List[NormalizedEvent] = []
    role = get(message, "role")
    content = get(message, "content")

    role_to_type: dict[str, NormalizedType] = {
        "user": "user_message",
        "assistant": "assistant_message",
        "system": "system_message",
    }

    # Simple string content -> visible message
    if isinstance(content, str):
        ev_type = role_to_type.get(role)
        if ev_type is not None:
            events.append(NormalizedEvent(type=ev_type, content=content))
    # List content -> iterate blocks
    elif isinstance(content, list):
        for block in content:
            btype = get(block, "type")

            # Visible text blocks
            if btype in (None, "text"):
                txt = get(block, "text") or get(block, "content")
                ev_type = role_to_type.get(role)
                if ev_type is not None:
                    events.append(NormalizedEvent(type=ev_type, content=txt))
            # Hidden reasoning
            elif btype == "thinking":
                signature = get(block, "signature")
                thinking_text = get(block, "thinking")
                cref = f"anthropic:thinking:{signature}" if signature else "anthropic:thinking"
                events.append(
                    NormalizedEvent(
                        type="reasoning",
                        content=thinking_text,
                        content_reference=cref,
                    )
                )
            # Redacted hidden reasoning
            elif btype == "redacted_thinking":
                thinking_text = get(block, "data")
                events.append(
                    NormalizedEvent(
                        type="reasoning",
                        content=thinking_text,
                    )
                )
            # Tool call
            elif btype == "tool_use":
                name = get(block, "name")
                tool_id = get(block, "id")
                tool_input = get(block, "input")
                args_str = json.dumps(tool_input or {}, ensure_ascii=False, separators=(",", ":"))
                events.append(
                    NormalizedEvent(
                        type="tool_call",
                        name=name,
                        content=args_str,
                        call_id=tool_id,
                    )
                )
            # Tool result (Anthropic delivers these in a 'user' role wrapper)
            elif btype == "tool_result":
                tool_use_id = get(block, "tool_use_id")
                block_content = get(block, "content")
                tr_content = ""
                if isinstance(block_content, str):
                    tr_content = block_content
                elif isinstance(block_content, list):
                    for part in block_content:
                        if part.get("type") == "text":
                            tr_content += f"{part.get('text')}\n"
                events.append(
                    NormalizedEvent(
                        type="tool_result",
                        content=tr_content.strip(),
                        call_id=tool_use_id,
                    )
                )
            else:
                raise ValueError(f"Unknown block type: {btype}")
    else:
        raise ValueError(f"Unknown content type: {type(content)}")

    return events

if __name__ == "__main__":
    from anthropic.types.beta import BetaThinkingBlock, BetaToolUseBlock
    raw = [{'content': "I'm at the Ferry Building in San Francisco. Use the tools to: "
                '(1) get current weather, and (2) suggest 2 good cafés nearby. '
                'Then give me a 1-paragraph plan.',
    'role': 'user'},
    {'content': [BetaThinkingBlock(signature='EtgECkYICRgCKkDcm4hfdHjRJyAuiQlOh5hrYZSl0dcL6wHrZeVtdCkKbeWATJtbn5/0/Ifj2A59gyPv/0HW0Mfgu2LSsWfmNX+YEgzXh6l/3hBzFU1wYI0aDOcAjbfFrx6miY9vXiIweQYY3luw8Jf0JXkAhp5QpxmNSLpGE2r/DPOqh5VYNbM+D1Sxka3v4WuEQIiczwK7Kr8Dsh3ViLRLECrs7xjlUuzfwSo9MSLe03tYn8HKrs7NKridXJRSIhjL8CX3u08IdPq1/jkflQAZ2Kuug7Ph5MOyITE9xh+yfpqekk1srpnupbPzUzbpUtLQJswkAYuuBqmbiF+zLJGegNZWAFYgkvKaYBx4HxGoe7gu3k3rUML6+5G5hdlQczOW22tgj/4x5G8zM3PLAYNHFALVkXMy8betyhr8HrDiAyY9+KqNkAmCvImTmpYXOtU4vmbJ4Fv9+Zp/MPw+iukhBeGzJ+KjeEOb65D+8/8fJJJP5fAh5R9vIfLPJY31XxA6nFGM/Xna6DzMT3dw27t9XGprJLOzA/E+IGDgGJ8l6/+BVj4VIr8kZJ1lcYeu3dWujQiciMUcjBi/gLLZLYb09x8Wh9HrYhWTLEXXZnsrbDyPItpglF665mhUBEg3ZFXTamY6lBKKuAyvZVOFuS6mfgNRFJB/CXo9jOFelx+mHcIYKMRgL2vGYaymGQeZnLSVKNwozYmneCqcW9aa5/8EmPPk4Ay7b8ZN4DifoVqpTIXijWPnyHXyp2zCZx8SDnPCW/EZtge+APhv4g9Fhw8SbuRx5HYlgIJ9GAE=', thinking='The user wants me to:\n1. Get current weather for San Francisco (they\'re at the Ferry Building)\n2. Find 2 cafés near the Ferry Building in San Francisco\n3. Provide a 1-paragraph plan based on this information\n\nI have both tools available and can call them simultaneously since they don\'t depend on each other. Let me make both calls:\n- get_weather for San Francisco\n- find_cafes near "Ferry Building, San Francisco" with limit of 2', type='thinking'),
                BetaToolUseBlock(id='toolu_01LGnrWwnnQSDY5V5pZDrh8m', input={'location': 'San Francisco'}, name='get_weather', type='tool_use'),
                BetaToolUseBlock(id='toolu_01MSkzmTN6GJqcSqzFAyfedH', input={'near': 'Ferry Building, San Francisco', 'limit': 2}, name='find_cafes', type='tool_use')],
    'role': 'assistant'},
    {'content': [{'content': '{"location": "San Francisco", "temp": 18, "unit": '
                            '"celsius", "condition": "Cloudy"}',
                'tool_use_id': 'toolu_01LGnrWwnnQSDY5V5pZDrh8m',
                'type': 'tool_result'},
                {'content': '{"near": "Ferry Building, San Francisco", '
                            '"results": ["Blue Bottle (66 Mint St)", "Sightglass '
                            '(270 7th St)"]}',
                'tool_use_id': 'toolu_01MSkzmTN6GJqcSqzFAyfedH',
                'type': 'tool_result'}],
    'role': 'user'}]

    for message in raw:
        normalized = serialize_anthropic_message(message)
        print(normalized)
