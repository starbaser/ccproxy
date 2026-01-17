#!/usr/bin/env python3
"""Example using Anthropic SDK with Z.AI GLM models via ccproxy.

Demonstrates routing GLM-4.7 requests through ccproxy. The proxy handles
authentication via ZAI_API_KEY configured in ~/.ccproxy/config.yaml.

Requirements:
- ccproxy running: `ccproxy start --detach`
- ZAI_API_KEY configured in environment (for config.yaml)
- glm-4.7 model defined in ~/.ccproxy/config.yaml
"""

import anthropic
from rich.console import Console
from rich.panel import Panel

console = Console()
err_console = Console(stderr=True)


def create_client() -> anthropic.Anthropic:
    """Create Anthropic client configured for ccproxy."""
    return anthropic.Anthropic(
        api_key="sk-proxy-dummy",  # Dummy key - ccproxy handles real auth
        base_url="http://127.0.0.1:4000",
    )


def simple_request() -> None:
    """Simple non-streaming request."""
    console.print(Panel("[cyan]Simple Request Example[/cyan]", border_style="blue"))

    client = create_client()

    response = client.messages.create(
        messages=[{"role": "user", "content": "Hello, can you tell me a short joke?"}],
        model="glm-4.7",
        max_tokens=100,
    )

    console.print("[green]Response:[/green]")
    console.print(response.content[0].text)
    console.print(
        f"\n[dim]Tokens: {response.usage.input_tokens} in, {response.usage.output_tokens} out[/dim]"
    )


def streaming_request() -> None:
    """Streaming request example."""
    console.print(Panel("[cyan]Streaming Request Example[/cyan]", border_style="blue"))

    client = create_client()

    console.print("[green]Response:[/green] ", end="")

    with client.messages.stream(
        messages=[{"role": "user", "content": "Count from 1 to 5."}],
        model="glm-4.7",
        max_tokens=100,
    ) as stream:
        for text in stream.text_stream:
            console.print(text, end="")

    console.print("\n")


def main() -> None:
    """Run examples."""
    try:
        console.print("[yellow]Note:[/yellow] Using GLM-4.7 via ccproxy\n")

        simple_request()
        console.print()

        streaming_request()

    except anthropic.APIError as e:
        err_console.print(f"[bold red]API Error:[/bold red] {e}")
        console.print(
            "\n[yellow]Troubleshooting:[/yellow]",
            "1. Start ccproxy: [cyan]ccproxy start --detach[/cyan]",
            "2. Verify glm-4.7 in ~/.ccproxy/config.yaml",
            "3. Ensure ZAI_API_KEY is set in environment",
            sep="\n",
        )
        raise


if __name__ == "__main__":
    main()
