from dataclasses import dataclass
from typing import List, Literal, Optional, Sequence, Union

from .models import NormalizedEvent
from .utils import get


@dataclass
class UserMessage:
    content: str
    type: Literal['user_message'] = 'user_message'

@dataclass
class AssistantMessage:
    content: str
    type: Literal['assistant_message'] = 'assistant_message'

@dataclass
class SystemMessage:
    content: str
    type: Literal['system_message'] = 'system_message'

@dataclass
class ReasoningItem:
    content: Optional[str] = None
    content_reference: Optional[str] = None
    summary: Optional[str] = None
    type: Literal['reasoning'] = 'reasoning'

@dataclass
class ToolCall:
    name: str
    content: str
    call_id: str
    type: Literal['tool_call'] = 'tool_call'

@dataclass
class ToolResult:
    content: str
    call_id: str
    type: Literal['tool_result'] = 'tool_result'

UploadType = Union[UserMessage, AssistantMessage, SystemMessage, ReasoningItem, ToolCall, ToolResult]

def base_to_normalized(events: Sequence[UploadType]) -> List[NormalizedEvent]:
    normalized_events: List[NormalizedEvent] = []
    for event in events:
        normalized_events.append(NormalizedEvent(
            type=get(event, 'type'),
            content=get(event, 'content'),
            content_reference=get(event, 'content_reference'),
            summary=get(event, 'summary'),
            name=get(event, 'name'),
            call_id=get(event, 'call_id'),
        ))
    return normalized_events
