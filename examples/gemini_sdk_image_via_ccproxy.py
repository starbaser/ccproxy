#!/usr/bin/env python3
"""google-genai SDK with multi-MB image payload through ccproxy.

Demonstrates the Glass-equivalent capability: large inline image data flows
through ccproxy unchanged because:

1. mitmproxy buffers full request bodies (``stream_large_bodies`` not set)
2. The redirect transform mode does NOT touch ``flow.request.content``
3. The ``gemini_cli`` hook merges the user payload into the v1internal envelope
   without re-encoding the inlineData base64 strings
4. JSON serialization handles arbitrary string sizes natively

Pass an image path as the first arg, or default to a synthetic test image.

Prereqs:
    * ccproxy running on port 4000
    * Valid Gemini OAuth creds at ``~/.gemini/oauth_creds.json``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from google import genai
from google.genai import types
from rich.console import Console
from rich.panel import Panel

console = Console()

CCPROXY_BASE = os.environ.get("CCPROXY_BASE_URL", "http://127.0.0.1:4000")


def make_client() -> genai.Client:
    return genai.Client(
        api_key="sk-ant-oat-ccproxy-gemini",
        http_options=types.HttpOptions(base_url=f"{CCPROXY_BASE}/gemini"),
    )


def analyze_image(path: Path) -> None:
    console.print(Panel(f"[cyan]Analyzing {path.name} ({path.stat().st_size / 1024:.1f} KB)[/cyan]", border_style="blue"))

    client = make_client()
    image_bytes = path.read_bytes()
    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents=[
            "Describe this image in one sentence.",
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
        ],
    )
    console.print("[green]Response:[/green]", response.text)


def main() -> None:
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
        if not path.exists():
            console.print(f"[red]File not found: {path}[/red]")
            sys.exit(1)
    else:
        console.print("[yellow]Usage: gemini_sdk_image_via_ccproxy.py <image-path>[/yellow]")
        console.print("[dim]Example: gemini_sdk_image_via_ccproxy.py ~/pictures/screenshot.png[/dim]")
        sys.exit(1)

    try:
        analyze_image(path)
    except Exception:
        console.print(
            "\n[yellow]Troubleshooting:[/yellow]",
            "1. Start ccproxy: [cyan]just up[/cyan]",
            "2. Verify Gemini creds: [cyan]gemini -p ''[/cyan]",
            "3. Check logs: [cyan]ccproxy logs -f[/cyan]",
            sep="\n",
        )
        raise


if __name__ == "__main__":
    main()
