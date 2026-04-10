"""Patch loader — imports patch modules and returns their apply functions."""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ccproxy.handler import CCProxyHandler

logger = logging.getLogger(__name__)

PatchFn = Callable[["CCProxyHandler"], None]


def load_patches(patch_paths: list[str]) -> list[PatchFn]:
    patches: list[PatchFn] = []
    for path in patch_paths:
        try:
            mod = importlib.import_module(path)
        except ImportError:
            logger.error("Failed to import patch module: %s", path)
            continue

        apply_fn = getattr(mod, "apply", None)
        if not callable(apply_fn):
            logger.warning("Patch module %s has no apply() function", path)
            continue

        patches.append(apply_fn)  # pyright: ignore[reportArgumentType]

    return patches
