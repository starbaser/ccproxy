"""Validate Gemini context caching via ccproxy's lightllm context_cache module.

Calls resolve_cached_content() against the live Google AI Studio API to
create/find a cached content resource, then makes a generateContent call
with the cached_content name to confirm the provider accepts it.

Requires a Gemini API key (resolved from ccproxy's oat_sources config).

Usage:
    uv run python scripts/test_gemini_cache.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import httpx
from rich.console import Console
from rich.table import Table

from ccproxy.config import get_config
from ccproxy.lightllm.context_cache import resolve_cached_content

console = Console()

LONG_TEXT = (
    "This is a comprehensive reference document about the history of computing. "
    "It covers topics from early mechanical calculators through modern quantum "
    "computing architectures. " * 200
)


def _get_gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    try:
        return subprocess.check_output(
            ["opc", "secret", "op://dev/gemini/credential"],
            text=True,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    # Fall back to ccproxy oat_sources
    config = get_config()
    token = config.get_oauth_token("gemini")
    if token:
        return token
    console.print("[red]Set GEMINI_API_KEY or configure opc/oat_sources[/red]")
    sys.exit(1)


def run() -> None:
    api_key = _get_gemini_key()
    model = "gemini-2.5-flash"

    messages = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "You are a helpful assistant."},
                {
                    "type": "text",
                    "text": LONG_TEXT,
                    "cache_control": {"type": "ephemeral"},
                },
            ],
        },
        {"role": "user", "content": "Summarize the above in one sentence."},
    ]

    table = Table(title="Gemini Context Cache Test")
    table.add_column("Step", width=30)
    table.add_column("Result")

    # Step 1: resolve (should create or find existing)
    console.print("\n[cyan]Step 1: resolve_cached_content (create/find)...[/cyan]")
    filtered_msgs, params, cached_name = resolve_cached_content(
        messages=messages,
        model=model,
        provider="gemini",
        optional_params={},
        api_key=api_key,
    )

    if cached_name is None:
        table.add_row("Cache resolution", "[red]FAILED — returned None[/red]")
        console.print(table)
        sys.exit(1)

    table.add_row("Cached content name", f"[green]{cached_name}[/green]")
    table.add_row("Filtered messages count", str(len(filtered_msgs)))
    table.add_row("Original messages count", str(len(messages)))

    # Step 2: resolve again (should be a cache hit)
    console.print("[cyan]Step 2: resolve_cached_content (lookup)...[/cyan]")
    _, _, cached_name_2 = resolve_cached_content(
        messages=messages,
        model=model,
        provider="gemini",
        optional_params={},
        api_key=api_key,
    )

    if cached_name_2 == cached_name:
        table.add_row("Cache hit on re-resolve", "[green]YES — same name[/green]")
    else:
        table.add_row("Cache hit on re-resolve", f"[yellow]Different: {cached_name_2}[/yellow]")

    # Step 3: make a generateContent call with the cached_content
    console.print("[cyan]Step 3: generateContent with cachedContent...[/cyan]")
    from ccproxy.lightllm.dispatch import _transform_gemini

    url, headers, body = _transform_gemini(
        model=model,
        provider="gemini",
        messages=filtered_msgs,
        optional_params={},
        api_key=api_key,
        cached_content=cached_name,
    )

    body_dict = json.loads(body)
    table.add_row("Request has cachedContent", str("cachedContent" in body_dict))

    try:
        resp = httpx.post(url, headers=headers, content=body, timeout=30.0)
        resp.raise_for_status()
        resp_data = resp.json()

        usage = resp_data.get("usageMetadata", {})
        table.add_row("Response status", f"[green]{resp.status_code}[/green]")
        table.add_row("Prompt tokens", str(usage.get("promptTokenCount", "?")))
        table.add_row("Cached content tokens", str(usage.get("cachedContentTokenCount", 0)))
        table.add_row("Output tokens", str(usage.get("candidatesTokenCount", "?")))

        cached_tokens = usage.get("cachedContentTokenCount", 0)
        if cached_tokens and cached_tokens > 0:
            table.add_row("Cache working", "[green bold]YES[/green bold]")
        else:
            table.add_row("Cache working", "[yellow]No cachedContentTokenCount in response[/yellow]")

    except httpx.HTTPStatusError as exc:
        table.add_row("Response status", f"[red]{exc.response.status_code}[/red]")
        table.add_row("Error", exc.response.text[:200])
    except httpx.HTTPError as exc:
        table.add_row("Error", f"[red]{exc}[/red]")

    console.print()
    console.print(table)


if __name__ == "__main__":
    run()
