"""Validate Anthropic prompt caching through ccproxy.

Sends two requests with cache_control annotations. The first should
show cache_creation_input_tokens > 0; the second should show
cache_read_input_tokens > 0 (cache hit).

Usage:
    uv run python scripts/test_anthropic_cache.py [--direct]

    --direct    Hit Anthropic API directly (bypass ccproxy)
"""

from __future__ import annotations

import os
import subprocess
import sys

import anthropic
from rich.console import Console
from rich.table import Table

console = Console()

CCPROXY_PORT = int(os.environ.get("CCPROXY_PORT", "4001"))
LONG_TEXT = (
    "This is a comprehensive reference document about the history of computing. "
    "It covers topics from early mechanical calculators through modern quantum "
    "computing architectures. " * 200
)


def _get_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        return subprocess.check_output(
            ["opc", "secret", "op://dev/anthropic/credential"],
            text=True,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        console.print("[red]Set ANTHROPIC_API_KEY or configure opc[/red]")
        sys.exit(1)


def run() -> None:
    direct = "--direct" in sys.argv
    api_key = _get_api_key()

    if direct:
        client = anthropic.Anthropic(api_key=api_key)
        console.print("[dim]Mode: direct to Anthropic API[/dim]")
    else:
        client = anthropic.Anthropic(
            base_url=f"http://127.0.0.1:{CCPROXY_PORT}",
            api_key=api_key,
        )
        console.print(f"[dim]Mode: through ccproxy at :{CCPROXY_PORT}[/dim]")

    messages_with_cache = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": LONG_TEXT,
                    "cache_control": {"type": "ephemeral"},
                },
                {
                    "type": "text",
                    "text": "Summarize the above in one sentence.",
                },
            ],
        },
    ]

    table = Table(title="Anthropic Prompt Cache Test")
    table.add_column("Request", width=10)
    table.add_column("Input Tokens", justify="right")
    table.add_column("Cache Write", justify="right")
    table.add_column("Cache Read", justify="right")
    table.add_column("Output Tokens", justify="right")

    for i in range(2):
        label = "1st (write)" if i == 0 else "2nd (read)"
        console.print(f"\n[cyan]Sending request {i + 1}...[/cyan]")

        try:
            resp = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=100,
                messages=messages_with_cache,
            )
        except anthropic.APIError as exc:
            console.print(f"[red]API error: {exc}[/red]")
            sys.exit(1)

        usage = resp.usage
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0

        table.add_row(
            label,
            str(usage.input_tokens),
            str(cache_write),
            str(cache_read),
            str(usage.output_tokens),
        )

    console.print()
    console.print(table)

    # Quick pass/fail
    console.print()
    if cache_read > 0:
        console.print("[green bold]Cache hit confirmed on second request[/green bold]")
    else:
        console.print("[yellow]No cache read tokens on second request — cache may not have been ready[/yellow]")


if __name__ == "__main__":
    run()
