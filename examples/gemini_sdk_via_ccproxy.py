#!/usr/bin/env python3
"""google-genai SDK through ccproxy using the Gemini sentinel key.

The sentinel key ``sk-ant-oat-ccproxy-gemini`` resolves to an OAuth Bearer
token from ``~/.gemini/oauth_creds.json`` via the ``forward_oauth`` hook.
The ``gemini_cli`` outbound hook then wraps the standard Gemini API body in
the v1internal envelope and routes the request to ``cloudcode-pa.googleapis.com``.

Prereqs:
    * ccproxy running on port 4000 (``ccproxy start`` or ``just up``)
    * Valid Gemini OAuth creds at ``~/.gemini/oauth_creds.json``
      (run ``gemini -p ""`` once to authenticate if missing)
"""

from __future__ import annotations

import os

from google import genai
from google.genai import types
from rich.console import Console
from rich.panel import Panel

console = Console()

CCPROXY_BASE = os.environ.get("CCPROXY_BASE_URL", "http://127.0.0.1:4000")


def make_client() -> genai.Client:
    """Build a Gemini client pointed at ccproxy with the sentinel key."""
    return genai.Client(
        api_key="sk-ant-oat-ccproxy-gemini",
        http_options=types.HttpOptions(base_url=f"{CCPROXY_BASE}/gemini"),
    )


def simple_request() -> None:
    console.print(Panel("[cyan]Simple Request[/cyan]", border_style="blue"))
    client = make_client()

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents="What is 2+2? Answer in one word.",
    )
    console.print("[green]Response:[/green]", response.text)


def streaming_request() -> None:
    console.print(Panel("[cyan]Streaming Request[/cyan]", border_style="blue"))
    client = make_client()

    console.print("[green]Response:[/green] ", end="")
    for chunk in client.models.generate_content_stream(
        model="gemini-3.1-pro-preview",
        contents="Count from 1 to 5, one number per line.",
    ):
        console.print(chunk.text, end="")
    console.print()


def main() -> None:
    try:
        simple_request()
        console.print()
        streaming_request()
    except Exception:
        console.print(
            "\n[yellow]Troubleshooting:[/yellow]",
            "1. Start ccproxy: [cyan]just up[/cyan] (or [cyan]ccproxy start[/cyan])",
            "2. Verify Gemini creds: [cyan]gemini -p ''[/cyan]",
            "3. Check logs: [cyan]ccproxy logs -f[/cyan]",
            "4. Inspect flow: [cyan]ccproxy flows compare[/cyan]",
            sep="\n",
        )
        raise


if __name__ == "__main__":
    main()
