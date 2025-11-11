#!/usr/bin/env python3
"""Example using Anthropic SDK with LiteLLM proxy (credentials config).

This example demonstrates using the Anthropic SDK pointed at the LiteLLM proxy
WITHOUT requiring an API key variable. The proxy handles authentication via
its credentials configuration.

This is the recommended approach when the proxy has credentials forwarding
enabled, as it eliminates the need to manage API keys in your scripts.

Note: We use a dummy API key because the SDK requires it for validation,
but the actual authentication is handled by the proxy's credentials config.
"""

import anthropic
from rich.console import Console
from rich.panel import Panel

console = Console()
err_console = Console(stderr=True)


def create_client() -> anthropic.Anthropic:
    """Create Anthropic client configured for ccproxy.

    The dummy API key satisfies SDK validation, but the proxy
    handles actual authentication via credentials configuration.
    """
    return anthropic.Anthropic(
        api_key="sk-proxy-dummy",  # Dummy key - proxy handles real auth
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
        console.print(
            f"\n[dim]Tokens: {response.usage.input_tokens} in, "
            f"{response.usage.output_tokens} out[/dim]"
        )

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
        console.print(
            "[yellow]Note:[/yellow] This script requires ccproxy running with "
            "credentials configuration.\n"
        )

        # Simple request
        simple_request()
        console.print()

        # Streaming request
        streaming_request()

    except Exception:
        console.print(
            "\n[yellow]Troubleshooting:[/yellow]",
            "1. Start ccproxy: [cyan]ccproxy start[/cyan]",
            "2. Verify credentials in ~/.ccproxy/ccproxy.yaml",
            "3. Check proxy logs: [cyan]ccproxy logs[/cyan]",
            sep="\n",
        )
        raise


if __name__ == "__main__":
    main()
