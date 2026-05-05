#!/usr/bin/env python3
"""Cross-provider transform via ccproxy's lightllm engine.

Uses the OpenAI Python SDK pointed at ccproxy. When the sentinel key resolves
to a provider whose wire format differs from OpenAI (``/v1/chat/completions``),
ccproxy auto-triggers a transform through LiteLLM's ``BaseConfig`` pipeline:

- Anthropic provider → ``AnthropicConfig.transform_request / transform_response``
- Gemini provider → ``_transform_gemini`` code path
  (bypasses ``BaseConfig``, uses ``_get_gemini_url`` + ``_transform_request_body``)

Streaming responses are handled by ``SseTransformer`` — provider-native SSE
chunks are parsed, transformed, and re-serialized as OpenAI-format SSE.

Requirements:
- ccproxy running: ``ccproxy start``
- ``providers.anthropic`` and ``providers.gemini`` configured in ``ccproxy.yaml``
"""

from __future__ import annotations

from openai import OpenAI
from rich.console import Console
from rich.panel import Panel

console = Console()
err_console = Console(stderr=True)

BASE_URL = "http://127.0.0.1:4000/v1"

SENTINEL_ANTHROPIC = "sk-ant-oat-ccproxy-anthropic"
SENTINEL_GEMINI = "sk-ant-oat-ccproxy-gemini"


def transform_to_anthropic() -> None:
    """OpenAI SDK → Anthropic via lightllm transform."""
    console.print(Panel("[cyan]OpenAI SDK → Anthropic (Transform)[/cyan]", border_style="blue"))

    client = OpenAI(api_key=SENTINEL_ANTHROPIC, base_url=BASE_URL)

    # Non-streaming
    console.print("[dim]Non-streaming:[/dim]")
    try:
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": "Hello, can you tell me a short joke?"}],
            model="claude-sonnet-4-5-20250929",
            max_tokens=100,
        )
        console.print(f"[green]Response:[/green] {response.choices[0].message.content}")
        console.print(f"[dim]Tokens: {response.usage.prompt_tokens} in, {response.usage.completion_tokens} out[/dim]")
    except Exception as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")

    console.print()

    # Streaming
    console.print("[dim]Streaming:[/dim]")
    try:
        stream = client.chat.completions.create(
            messages=[{"role": "user", "content": "Count from 1 to 5."}],
            model="claude-sonnet-4-5-20250929",
            max_tokens=100,
            stream=True,
        )
        console.print("[green]Response:[/green] ", end="")
        for chunk in stream:
            if chunk.choices[0].delta.content:
                console.print(chunk.choices[0].delta.content, end="")
        console.print("\n")
    except Exception as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")


def transform_to_gemini() -> None:
    """OpenAI SDK → Gemini via lightllm transform."""
    console.print(Panel("[cyan]OpenAI SDK → Gemini (Transform)[/cyan]", border_style="blue"))

    client = OpenAI(api_key=SENTINEL_GEMINI, base_url=BASE_URL)

    # Non-streaming
    console.print("[dim]Non-streaming:[/dim]")
    try:
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": "What is 2+2? Answer in one word."}],
            model="gemini-3.1-pro-preview",
            max_tokens=50,
        )
        console.print(f"[green]Response:[/green] {response.choices[0].message.content}")
        console.print(f"[dim]Tokens: {response.usage.prompt_tokens} in, {response.usage.completion_tokens} out[/dim]")
    except Exception as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")

    console.print()

    # Streaming
    console.print("[dim]Streaming:[/dim]")
    try:
        stream = client.chat.completions.create(
            messages=[{"role": "user", "content": "Count from 1 to 5, one per line."}],
            model="gemini-3.1-pro-preview",
            max_tokens=100,
            stream=True,
        )
        console.print("[green]Response:[/green] ", end="")
        for chunk in stream:
            if chunk.choices[0].delta.content:
                console.print(chunk.choices[0].delta.content, end="")
        console.print("\n")
    except Exception as e:
        err_console.print(f"[bold red]Error:[/bold red] {e}")


def main() -> None:
    """Run both transform examples."""
    try:
        console.print("[yellow]Note:[/yellow] This script requires ccproxy running: [cyan]ccproxy start[/cyan]\n")

        transform_to_anthropic()
        console.print()
        transform_to_gemini()

    except Exception:
        console.print(
            "\n[yellow]Troubleshooting:[/yellow]",
            "1. Start ccproxy: [cyan]ccproxy start[/cyan]",
            "2. Verify providers.anthropic and providers.gemini in ccproxy.yaml",
            "3. Check logs: [cyan]ccproxy logs -f[/cyan]",
            "4. Inspect flow: [cyan]ccproxy flows compare[/cyan]",
            sep="\n",
        )
        raise


if __name__ == "__main__":
    main()
