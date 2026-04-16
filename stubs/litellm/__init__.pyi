from typing import Any

class AuthenticationError(Exception): ...

class _LiteLLMUtils:
    def get_logging_id(self, start_time: Any, response_obj: Any) -> str | None: ...

utils: _LiteLLMUtils

async def acompletion(*args: Any, **kwargs: Any) -> Any: ...
