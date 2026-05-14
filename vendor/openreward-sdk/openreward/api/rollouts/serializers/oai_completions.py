import json
from typing import List, Literal, Sequence, TypedDict, Union

from .models import NormalizedEvent
from .utils import add_text_event


class OpenAIToolFunction(TypedDict, total=False):
    name: str
    arguments: str  # JSON string

class OpenAIToolCall(TypedDict, total=False):
    id: str
    type: Literal["function"]
    function: OpenAIToolFunction

class OpenAIChatUserMessage(TypedDict):
    role: Literal["user"]
    content: str

class OpenAIChatAssistantMessage(TypedDict, total=False):
    role: Literal["assistant"]
    content: str
    tool_calls: Sequence[OpenAIToolCall]

class OpenAIChatToolMessage(TypedDict):
    role: Literal["tool"]
    tool_call_id: str
    content: str  # JSON string (or text)

class OpenAIChatSystemMessage(TypedDict):
    role: Literal["system"]
    content: str

OpenAIChatMessage = Union[
    OpenAIChatUserMessage,
    OpenAIChatAssistantMessage,
    OpenAIChatToolMessage,
    OpenAIChatSystemMessage,
]


def openai_completions_to_normalized(completions: Sequence[OpenAIChatMessage]) -> List[NormalizedEvent]:
    """
    Normalize OpenAI Chat-style messages into a flat event timeline.

    Mapping rules:
      - role='user'/'assistant'/'syste' with string content -> corresponding *_message
      - assistant.tool_calls[] -> one tool_call per entry (content = arguments JSON string, name, call_id=id)
      - role='tool' -> tool_result (content, call_id = tool_call_id)
      - Empty/whitespace content is ignored for visible messages
    """
    events: List[NormalizedEvent] = []

    for msg in completions:
        role = msg.get("role")

        # Tool results
        if role == "tool":
            call_id = msg.get("tool_call_id")
            content = (msg.get("content") or "").strip()
            if content:
                try:
                    events.append(
                        NormalizedEvent(
                            type="tool_result",
                            content=content,
                            call_id=call_id,
                        )
                    )
                except Exception:
                    # If malformed, skip rather than crash normalization
                    pass
            continue

        # Assistant: tool calls + visible text
        if role == "assistant":
            tool_calls = msg.get("tool_calls") or []
            for tc in tool_calls:
                try:
                    func = (tc or {}).get("function") or {}
                    name = func.get("name")
                    args = func.get("arguments")
                    if not isinstance(args, str):
                        try:
                            args = json.dumps(args or {}, ensure_ascii=False, separators=(",", ":"))
                        except Exception:
                            args = str(args)
                    events.append(
                        NormalizedEvent(
                            type="tool_call",
                            name=name,
                            content=args or "",
                            call_id=(tc or {}).get("id"),
                        )
                    )
                except Exception:
                    # Skip invalid tool call entries
                    pass

            add_text_event(events, "assistant", msg.get("content"))
            continue

        # User / System visible messages
        if role in ("user", "system"):
            add_text_event(events, role, msg.get("content"))
            continue

        # Unknown roles are ignored silently

    return events


if __name__ == "__main__":
    completions = [{'role': 'user',
    'content': "I'm at the Ferry Building in San Francisco. Use the tools to: (1) get current weather, and (2) suggest 2 good cafés nearby. Then give me a 1-paragraph plan."},
    {'role': 'assistant',
    'content': '',
    'tool_calls': [{'id': 'call_l424lBzaUjflA9OkvV4Mz9i1',
        'type': 'function',
        'function': {'name': 'get_weather',
        'arguments': '{"location": "San Francisco, CA"}'}},
    {'id': 'call_pcg4BqfCU2JggMral3jIoUuB',
        'type': 'function',
        'function': {'name': 'find_cafes',
        'arguments': '{"near": "Ferry Building, San Francisco", "limit": 2}'}}]},
    {'role': 'tool',
    'tool_call_id': 'call_l424lBzaUjflA9OkvV4Mz9i1',
    'content': '{"location": "San Francisco, CA", "temp": 18, "unit": "celsius", "condition": "Cloudy"}'},
    {'role': 'tool',
    'tool_call_id': 'call_pcg4BqfCU2JggMral3jIoUuB',
    'content': '{"near": "Ferry Building, San Francisco", "results": ["Blue Bottle (66 Mint St)", "Sightglass (270 7th St)"]}'},
    {'role': 'assistant',
    'content': 'Here’s what I found:\n- Weather: 18°C, Cloudy in San Francisco.\n- Nearby cafés: Blue Bottle (66 Mint St) and Sightglass (270 7th St).\n\nPlan: Since it’s a cool, cloudy day, start with a short indoor browse through the Ferry Building Marketplace, then walk or rideshare to Blue Bottle for a quick espresso and pastry; if you want a change of scene, continue to Sightglass for a pour-over and to relax a bit longer. Wrap up by returning to the waterfront for bay views if the clouds lift, or duck into a nearby bar or bookstore if it stays gray.'}]

    normalized = openai_completions_to_normalized(completions)
    print(normalized)
