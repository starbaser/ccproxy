"""Override header parsing for x-ccproxy-hooks.

Allows SDK clients to control hook execution:
- +hook → Force run (skip guard)
- -hook → Force skip
- No prefix → Normal (guard decides)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class HookOverride(Enum):
    """Override mode for a hook."""

    NORMAL = "normal"  # Guard decides
    FORCE_RUN = "force_run"  # Skip guard, always run
    FORCE_SKIP = "force_skip"  # Skip this hook entirely


@dataclass
class OverrideSet:
    """Parsed override configuration.

    Attributes:
        overrides: Mapping of hook name to override mode
        raw_header: Original header value for debugging
    """

    overrides: dict[str, HookOverride]
    raw_header: str

    def get_override(self, hook_name: str) -> HookOverride:
        """Get override mode for a hook.

        Args:
            hook_name: Name of the hook

        Returns:
            Override mode (NORMAL if not specified)
        """
        return self.overrides.get(hook_name, HookOverride.NORMAL)

    def should_run(self, hook_name: str, guard_result: bool) -> bool:
        """Determine if a hook should run.

        Args:
            hook_name: Name of the hook
            guard_result: Result of the hook's guard function

        Returns:
            True if the hook should execute
        """
        override = self.get_override(hook_name)

        if override == HookOverride.FORCE_RUN:
            return True
        elif override == HookOverride.FORCE_SKIP:
            return False
        else:
            return guard_result


def parse_overrides(header_value: str | None) -> OverrideSet:
    """Parse x-ccproxy-hooks header value.

    Format: comma-separated list of hook overrides
    - +hook_name → Force run
    - -hook_name → Force skip
    - hook_name → Normal (same as not specifying)

    Args:
        header_value: Raw header value or None

    Returns:
        OverrideSet with parsed overrides

    Examples:
        >>> parse_overrides("+forward_oauth,-rule_evaluator")
        OverrideSet(overrides={'forward_oauth': FORCE_RUN, 'rule_evaluator': FORCE_SKIP}, ...)
        >>> parse_overrides(None)
        OverrideSet(overrides={}, raw_header='')
    """
    if not header_value:
        return OverrideSet(overrides={}, raw_header="")

    overrides: dict[str, HookOverride] = {}
    header_value = header_value.strip()

    for part in header_value.split(","):
        part = part.strip()
        if not part:
            continue

        if part.startswith("+"):
            hook_name = part[1:]
            if hook_name:
                overrides[hook_name] = HookOverride.FORCE_RUN
        elif part.startswith("-"):
            hook_name = part[1:]
            if hook_name:
                overrides[hook_name] = HookOverride.FORCE_SKIP
        else:
            # No prefix = normal (explicit declaration)
            overrides[part] = HookOverride.NORMAL

    if overrides:
        logger.debug("Parsed hook overrides: %s", overrides)

    return OverrideSet(overrides=overrides, raw_header=header_value)


def extract_overrides_from_context(headers: dict[str, str]) -> OverrideSet:
    """Extract and parse overrides from request headers.

    Args:
        headers: Request headers dict (case-insensitive keys expected)

    Returns:
        OverrideSet with parsed overrides
    """
    # Try various case combinations
    for key in ["x-ccproxy-hooks", "X-CCProxy-Hooks", "X-CCPROXY-HOOKS"]:
        if key in headers:
            return parse_overrides(headers[key])

    # Try lowercase lookup
    lower_headers = {k.lower(): v for k, v in headers.items()}
    header_value = lower_headers.get("x-ccproxy-hooks")

    return parse_overrides(header_value)
