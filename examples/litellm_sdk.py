#!/usr/bin/env python3
"""Example using LiteLLM Python SDK with proxy (credentials config).

This example demonstrates using litellm.acompletion() pointed at the ccproxy
WITHOUT requiring an API key variable. The proxy handles authentication via
its credentials configuration.

Note: The litellm.anthropic.messages interface bypasses proxies, so we use
the standard litellm.acompletion() interface instead.
"""

import asyncio
import litellm
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()
err_console = Console(stderr=True)


async def simple_request() -> None:
    """Simple non-streaming request."""
    console.print(Panel("[cyan]Simple Request Example[/cyan]", border_style="blue"))

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        transient=True,
    ) as progress:
        progress.add_task("Sending request...", total=None)

        # Use standard litellm.acompletion() with proxy
        # Dummy API key satisfies validation, proxy handles real auth
        response = await litellm.acompletion(
            messages=[{"role": "user", "content": "Hello, can you tell me a short joke?"}],
            model="claude-haiku-4-5-20251001",  # Use model defined in proxy config
            max_tokens=100,
            api_base="http://127.0.0.1:4000",
            api_key="sk-proxy-dummy",  # Dummy key - proxy handles real auth
        )

    console.print("[green]Response:[/green]")
    console.print(response.choices[0].message.content)
    console.print(
        f"\n[dim]Tokens: {response.usage.prompt_tokens} in, "
        f"{response.usage.completion_tokens} out[/dim]"
    )


async def streaming_request() -> None:
    """Streaming request example."""
    console.print(Panel("[cyan]Streaming Request Example[/cyan]", border_style="blue"))

    console.print("[green]Response:[/green] ", end="")

    # Streaming with litellm.acompletion()
    response = await litellm.acompletion(
        messages=[{"role": "user", "content": "Count from 1 to 5."}],
        model="claude-haiku-4-5-20251001",  # Use model defined in proxy config
        max_tokens=200,
        stream=True,
        api_base="http://127.0.0.1:4000",
        api_key="sk-proxy-dummy",  # Dummy key - proxy handles real auth
    )

    async for chunk in response:
        if chunk.choices[0].delta.content:
            console.print(chunk.choices[0].delta.content, end="")

    console.print("\n")


async def main() -> None:
    """Run examples."""
    try:
        # Simple request
        await simple_request()
        console.print()

        # Streaming request
        await streaming_request()

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}", style="red")
        console.print(
            "\n[yellow]Make sure:[/yellow]",
            "1. ccproxy is running: [cyan]ccproxy start[/cyan]",
            "2. Credentials are configured in ccproxy.yaml",
            sep="\n",
        )
        raise


if __name__ == "__main__":
    asyncio.run(main())
