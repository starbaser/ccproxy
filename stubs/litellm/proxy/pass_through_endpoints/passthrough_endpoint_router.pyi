from typing import Any

class PassthroughEndpointRouter:
    def get_credentials(
        self,
        custom_llm_provider: str,
        region_name: Any,
    ) -> str | None: ...
