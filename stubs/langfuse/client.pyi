from enum import Enum
from typing import Any

class StateType(Enum):
    OBSERVATION = "OBSERVATION"
    TRACE = "TRACE"

class StatefulGenerationClient:
    def __init__(
        self,
        client: Any,
        id: str,
        state_type: StateType,
        trace_id: str,
        task_manager: Any,
        **kwargs: Any,
    ) -> None: ...
    def update(self, **kwargs: Any) -> None: ...
