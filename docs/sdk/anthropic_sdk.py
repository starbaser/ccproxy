#!/usr/bin/env python3
"""Example using Anthropic SDK with ccproxy OAuth sentinel key.

This example demonstrates using the Anthropic SDK with ccproxy's OAuth
sentinel key feature. The sentinel key `sk-ant-oat-ccproxy-{provider}`
triggers automatic OAuth token substitution from ccproxy's cached credentials.

Requirements:
- ccproxy running: `ccproxy start --detach`
- OAuth credentials configured in ~/.ccproxy/ccproxy.yaml under oat_sources
"""

import anthropic
from rich.console import Console
from rich.panel import Panel

console = Console()
err_console = Console(stderr=True)

# OAuth sentinel key - ccproxy substitutes this with real OAuth token
SENTINEL_KEY = "sk-ant-oat-ccproxy-anthropic"


def create_client() -> anthropic.Anthropic:
    """Create Anthropic client configured for ccproxy with OAuth sentinel key.

    The sentinel key triggers OAuth token substitution in ccproxy's pipeline hooks,
    which also inject required headers and system message prefix.
    """
    return anthropic.Anthropic(
        api_key=SENTINEL_KEY,
        base_url="http://127.0.0.1:4000",
    )


def simple_request() -> None:
    """Simple non-streaming request."""
    console.print(Panel("[cyan]Simple Request Example[/cyan]", border_style="blue"))

    client = create_client()

    try:
        response = client.messages.create(
            messages=[{"role": "user", "content": "Hello, can you tell me a short joke?"}],
            model="claude-sonnet-4-5-20250929",
            max_tokens=100,
        )

        console.print("[green]Response:[/green]")
        console.print(response.content[0].text)
        console.print(f"\n[dim]Tokens: {response.usage.input_tokens} in, {response.usage.output_tokens} out[/dim]")

    except anthropic.APIError as e:
        err_console.print(f"[bold red]API Error:[/bold red] {e}")
        raise


def streaming_request() -> None:
    """Streaming request example."""
    console.print(Panel("[cyan]Streaming Request Example[/cyan]", border_style="blue"))

    client = create_client()

    try:
        console.print("[green]Response:[/green] ", end="")

        with client.messages.stream(
            messages=[{"role": "user", "content": "Count from 1 to 5."}],
            model="claude-sonnet-4-5-20250929",
            max_tokens=100,
        ) as stream:
            for text in stream.text_stream:
                console.print(text, end="")

        console.print("\n")

    except anthropic.APIError as e:
        err_console.print(f"[bold red]API Error:[/bold red] {e}")
        raise


def main() -> None:
    """Run examples."""
    try:
        # Check if running
        console.print("[yellow]Note:[/yellow] This script requires ccproxy running: [cyan]ccproxy start --detach[/cyan]\n")

        # Simple request
        simple_request()
        console.print()

        # Streaming request
        streaming_request()

    except Exception:
        console.print(
            "\n[yellow]Troubleshooting:[/yellow]",
            "1. Start ccproxy: [cyan]ccproxy start --detach[/cyan]",
            "2. Verify oat_sources in ~/.ccproxy/ccproxy.yaml",
            "3. Check logs: [cyan]ccproxy logs -f[/cyan]",
            sep="\n",
        )
        raise


if __name__ == "__main__":
    main()
