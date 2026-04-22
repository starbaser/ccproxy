"""ShapeStore — per-provider on-disk store of captured request shapes.

One ``.mflow`` file per provider under ``shapes_dir``. Append on shape,
read all on pick. Files are native mitmproxy tnetstring dumps, openable
in ``mitmweb --rfile``.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from mitmproxy import http
from mitmproxy.io import FlowReader, FlowWriter

from ccproxy.config import get_config, get_config_dir

logger = logging.getLogger(__name__)


class ShapeStore:
    """Thread-safe per-provider store of captured request shapes."""

    def __init__(self, shapes_dir: Path) -> None:
        self._dir = shapes_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def add(self, provider: str, flow: http.HTTPFlow) -> None:
        """Append a flow to the provider's shape file."""
        path = self._path(provider)
        with self._lock, path.open("ab") as fo:
            FlowWriter(fo).add(flow)  # type: ignore[no-untyped-call]
        logger.info("Saved shape for flow %s under provider %s", flow.id, provider)

    def pick(self, provider: str) -> http.HTTPFlow | None:
        """Return the most recently added shape for the provider, or None."""
        path = self._path(provider)
        if not path.exists():
            return None
        flows: list[http.HTTPFlow] = []
        with self._lock, path.open("rb") as fo:
            for f in FlowReader(fo).stream():  # type: ignore[no-untyped-call]
                if isinstance(f, http.HTTPFlow):
                    flows.append(f)
        return flows[-1] if flows else None

    def clear(self, provider: str) -> None:
        """Delete the provider's shape file, if any."""
        with self._lock:
            self._path(provider).unlink(missing_ok=True)

    def list_providers(self) -> list[str]:
        """Return sorted list of providers with at least one shape file."""
        with self._lock:
            return sorted(p.stem for p in self._dir.glob("*.mflow"))

    def _path(self, provider: str) -> Path:
        return self._dir / f"{provider}.mflow"


# --- Singleton ---

_store_instance: ShapeStore | None = None
_store_lock = threading.Lock()


def get_store() -> ShapeStore:
    global _store_instance
    if _store_instance is None:
        with _store_lock:
            if _store_instance is None:
                _store_instance = _create_store()
    return _store_instance


def _create_store() -> ShapeStore:
    config = get_config()
    config_dir = get_config_dir()

    if config.shaping.shapes_dir:
        shapes_dir = Path(config.shaping.shapes_dir).expanduser()
    else:
        shapes_dir = config_dir / "shaping" / "shapes"

    return ShapeStore(shapes_dir=shapes_dir)


def clear_store_instance() -> None:
    """Reset the singleton (for tests)."""
    global _store_instance
    _store_instance = None
