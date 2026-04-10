from typing import Any

ILLEGAL_DISPLAY_PARAMS: list[str]

def _update_litellm_params_for_health_check(
    model_info: dict[str, Any],
    litellm_params: dict[str, Any],
) -> dict[str, Any]: ...
