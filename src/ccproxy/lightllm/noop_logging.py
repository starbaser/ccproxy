"""Duck-type stub for litellm's Logging class.

BaseConfig.transform_response() takes a ``logging_obj`` parameter typed as
``Any`` at runtime.  The only method it calls is ``post_call()`` — everything
else (cost tracking, callbacks, caching) lives in the real Logging class,
which we intentionally bypass.
"""

from __future__ import annotations

from typing import Any


class NoopLogging:
    model_call_details: dict[str, Any]

    def __init__(self) -> None:
        self.model_call_details = {}

    def pre_call(self, *a: Any, **kw: Any) -> None: ...
    def post_call(self, *a: Any, **kw: Any) -> None: ...
