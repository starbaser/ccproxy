from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any


class Loader:
    def add_option(
        self,
        name: str,
        typespec: type,
        default: Any,
        help: str,
        choices: Sequence[str] | None = ...,
    ) -> None: ...

    def add_command(self, path: str, func: Callable[..., Any]) -> None: ...
