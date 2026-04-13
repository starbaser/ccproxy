"""Verify CCH billing header hash algorithm against live intercepted flows.

Fetches all flows from mitmweb, extracts the billing header from system[0],
extracts the first user message text, recomputes the CCH hash, and compares.

Usage:
    uv run python scripts/verify_cch.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

from rich.console import Console
from rich.table import Table

from ccproxy.tools.flows import MitmwebClient, _make_client

console = Console()

# Known salt for Claude Code v2.1.87 (from cch.md analysis)
KNOWN_SALT = "59cf53e54c78"
KNOWN_VERSION = "2.1.87"
SAMPLE_POSITIONS = (4, 7, 20)

BILLING_RE = re.compile(
    r"x-anthropic-billing-header:\s*"
    r"cc_version=(?P<version>[^;]+);\s*"
    r"cc_entrypoint=(?P<entrypoint>[^;]+);\s*"
    r"cch=(?P<cch>[^;]+);"
)


def compute_cch(salt: str, user_text: str, version_base: str) -> str:
    """Reimplement x46() from Claude Code."""
    chars = "".join(
        user_text[i] if i < len(user_text) else "0"
        for i in SAMPLE_POSITIONS
    )
    preimage = f"{salt}{chars}{version_base}"
    return hashlib.sha256(preimage.encode()).hexdigest()[:3]


def extract_first_user_text(messages: list[dict]) -> str:
    """Extract text from the first user message."""
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    return str(block.get("text", ""))
    return ""


def extract_billing_header(system: list | str | None) -> dict | None:
    """Parse the billing header from system content blocks."""
    if not isinstance(system, list):
        return None
    for block in system:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text", "")
        match = BILLING_RE.search(text)
        if match:
            return {
                "raw_text": text,
                "version": match.group("version"),
                "entrypoint": match.group("entrypoint"),
                "cch": match.group("cch"),
                "cache_control": block.get("cache_control"),
            }
    return None


def main() -> None:
    client = _make_client()
    flows = client.list_flows()

    if not flows:
        console.print("[yellow]No flows captured. Run claude through the inspector first.[/yellow]")
        sys.exit(1)

    results_table = Table(title="CCH Hash Verification")
    results_table.add_column("Flow", width=8)
    results_table.add_column("cc_version", width=16)
    results_table.add_column("Actual Suffix", width=8)
    results_table.add_column("Computed", width=8)
    results_table.add_column("Match", width=6)
    results_table.add_column("Sampled Chars", width=15)
    results_table.add_column("User Text (first 40)", max_width=40)

    found = 0
    matched = 0

    for flow in flows:
        flow_id = flow["id"]
        req = flow["request"]

        # Only look at Anthropic API requests
        host = req.get("pretty_host", "")
        if "anthropic" not in host and "claude" not in host:
            continue

        try:
            body_raw = client.get_request_body(flow_id)
            body = json.loads(body_raw)
        except Exception:
            continue

        system = body.get("system")
        messages = body.get("messages", [])
        billing = extract_billing_header(system)

        if billing is None:
            continue

        found += 1
        user_text = extract_first_user_text(messages)

        # Parse version suffix: "2.1.87.6d6" -> base "2.1.87", suffix "6d6"
        version_parts = billing["version"].rsplit(".", 1)
        if len(version_parts) == 2:
            # Could be "2.1.87.6d6" -> ["2.1.87", "6d6"]
            # But also "2.1.87" has dots. The suffix is always 3 hex chars at the end.
            full_ver = billing["version"]
            # The hash is the last dot-segment if it's 3 hex chars
            last_seg = full_ver.rsplit(".", 1)[-1]
            if re.fullmatch(r"[0-9a-f]{3}", last_seg):
                actual_suffix = last_seg
                version_base = full_ver[:-(len(last_seg) + 1)]  # strip ".xyz"
            else:
                actual_suffix = "???"
                version_base = full_ver
        else:
            actual_suffix = "???"
            version_base = billing["version"]

        computed = compute_cch(KNOWN_SALT, user_text, version_base)

        # Also try with the full version string in case algo uses it differently
        computed_full = compute_cch(KNOWN_SALT, user_text, full_ver)

        is_match = computed == actual_suffix
        if is_match:
            matched += 1
            match_style = "[green]YES[/green]"
        elif computed_full == actual_suffix:
            matched += 1
            match_style = "[green]YES*[/green]"
            computed = computed_full
        else:
            match_style = "[red]NO[/red]"

        sampled_chars = "".join(
            user_text[i] if i < len(user_text) else "0"
            for i in SAMPLE_POSITIONS
        )

        results_table.add_row(
            flow_id[:8],
            billing["version"],
            actual_suffix,
            computed,
            match_style,
            repr(sampled_chars),
            user_text[:40] if user_text else "[dim](empty)[/dim]",
        )

        # Print detailed debug for first few
        if found <= 3:
            console.print(f"\n[bold]Flow {flow_id[:8]}[/bold]")
            console.print(f"  Billing text: [cyan]{billing['raw_text']}[/cyan]")
            console.print(f"  cache_control: {billing['cache_control']}")
            console.print(f"  Version base: {version_base}")
            console.print(f"  User text length: {len(user_text)}")
            console.print(f"  Sampled chars [{SAMPLE_POSITIONS}]: {sampled_chars!r}")
            preimage = f"{KNOWN_SALT}{sampled_chars}{version_base}"
            full_hash = hashlib.sha256(preimage.encode()).hexdigest()
            console.print(f"  Preimage: {preimage!r}")
            console.print(f"  SHA256: {full_hash}")
            console.print(f"  First 3 hex: {full_hash[:3]}")
            console.print(f"  Actual suffix: {actual_suffix}")

    if found == 0:
        console.print("[yellow]No flows with billing headers found.[/yellow]")
        console.print("Run: ccproxy run --inspect -- claude -p 'your prompt here'")
        sys.exit(1)

    console.print()
    console.print(results_table)
    console.print(f"\n[bold]Summary:[/bold] {matched}/{found} hashes verified")


if __name__ == "__main__":
    main()
