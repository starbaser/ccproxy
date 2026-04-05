# Type stubs for litellm
from typing import Any

class AuthenticationError(Exception):
    status_code: int
    message: str

class _LiteLLMUtils:
    def get_logging_id(self, start_time: Any, response_obj: Any) -> str | None: ...

utils: _LiteLLMUtils

async def acompletion(**kwargs: Any) -> Any: ...
