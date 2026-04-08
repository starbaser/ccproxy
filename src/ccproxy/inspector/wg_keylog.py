"""WireGuard key export for Wireshark decryption. **NOT** a "keylogger"
Reads mitmproxy's WireGuard keypair JSON and writes a Wireshark-compatible
keylog file (wg.keylog_file format) for decrypting the outer WireGuard
tunnel layer in packet captures.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def write_wg_keylog(wg_conf_path: Path, output_path: Path) -> bool:
    """Read WireGuard keypair JSON and write Wireshark keylog file.

    The keylog format is documented in Wireshark's WireGuard dissector.
    Each line: LOCAL_STATIC_PRIVATE_KEY = <base64>

    Returns True on success, False on failure.
    """
    if not wg_conf_path.exists():
        logger.debug("WireGuard config not found: %s", wg_conf_path)
        return False

    try:
        data = json.loads(wg_conf_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read WireGuard config %s: %s", wg_conf_path, e)
        return False

    server_key = data.get("server_key")
    client_key = data.get("client_key")

    if not server_key:
        logger.warning("No server_key in WireGuard config: %s", wg_conf_path)
        return False

    lines = [f"LOCAL_STATIC_PRIVATE_KEY = {server_key}"]
    if client_key:
        lines.append(f"LOCAL_STATIC_PRIVATE_KEY = {client_key}")

    output_path.write_text("\n".join(lines) + "\n")
    logger.info("WireGuard keylog written to %s", output_path)
    return True
