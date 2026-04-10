from typing import Any

config_path: str | None
app: Any

class _LLMRouter:
    def get_model_list(self) -> list[dict[str, Any]] | None: ...

llm_router: _LLMRouter | None
