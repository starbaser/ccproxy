from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, ClassVar


@dataclass
class Error:
    msg: str
    timestamp: float = field(default_factory=time.time)
    KILLED_MESSAGE: ClassVar[str]


class Flow:
    id: str
    error: Error | None
    intercepted: bool
    marked: str
    is_replay: str | None
    live: bool
    timestamp_created: float
    metadata: dict[str, Any]
