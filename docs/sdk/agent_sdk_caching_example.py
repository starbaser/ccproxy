"""Agent SDK caching example with ccproxy OAuth sentinel key.

This example demonstrates using Claude Agent SDK with ccproxy's OAuth
sentinel key feature to monitor prompt caching metrics. It creates a
substantial prompt with context to trigger caching and prints detailed
usage statistics including cache hits.

Purpose:
    - Demonstrate Agent SDK query() with ccproxy OAuth integration
    - Monitor prompt caching effectiveness via usage stats
    - Show how to handle message types and extract metrics

Usage:
    1. Start ccproxy with MITM enabled:
       ccproxy start --detach --mitm
       ccproxy logs -f

    2. In another terminal, run this example:
       uv run python docs/sdk/agent_sdk_caching_example.py

    3. Run multiple times to observe cache hit metrics in logs

    4. Stop ccproxy when done:
       ccproxy stop

Cache Monitoring:
    - First run: Creates cache with substantial context (>1024 tokens)
    - Subsequent runs: Should hit cache, reducing input tokens
    - Monitor ccproxy logs for cache_creation_input_tokens and cache_read_input_tokens
    - ResultMessage.usage will show cache metrics if available

Environment Variables:
    ANTHROPIC_BASE_URL: Points to ccproxy (http://localhost:4000)
    ANTHROPIC_API_KEY: OAuth sentinel key (sk-ant-oat-ccproxy-anthropic)
"""

import asyncio
import os
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

# Configure ccproxy with OAuth sentinel key
os.environ["ANTHROPIC_BASE_URL"] = "http://localhost:4000"
os.environ["ANTHROPIC_API_KEY"] = "sk-ant-oat-ccproxy-anthropic"

# Note: claude_agent_sdk must be installed in the same environment
# Install with: uv add claude-agent-sdk
from claude_agent_sdk import (  # type: ignore[import-not-found]
    query,
    ClaudeAgentOptions,
    AssistantMessage,
    ResultMessage,
    TextBlock,
)

console = Console()


async def main() -> None:
    """Execute Agent SDK query with substantial context for caching."""
    # Create substantial prompt with context to trigger caching (>1024 tokens)
    context = """
    You are analyzing a Python proxy server project called ccproxy that routes
    Claude Code requests to different LLM providers. The architecture includes:

    1. CCProxyHandler - LiteLLM CustomLogger that intercepts all requests
    2. RequestClassifier - Rule-based evaluation system (first match wins)
    3. ModelRouter - Maps rule names to model configurations
    4. Hook Pipeline - Sequential execution of configured hooks

    Key Components:
    - handler.py: Main entry point, orchestrates classification via async_pre_call_hook()
    - classifier.py: Rule-based classification system
    - rules.py: ClassificationRule base class and built-in rules:
      * ThinkingRule - Matches requests with "thinking" field
      * MatchModelRule - Matches by model name substring
      * MatchToolRule - Matches by tool name in request
      * TokenCountRule - Evaluates based on token count threshold
    - router.py: Model configuration management from LiteLLM proxy
    - config.py: Pydantic-based configuration with multi-level discovery
    - hooks.py: Built-in hooks for request processing:
      * rule_evaluator - Evaluates rules and stores routing decision
      * model_router - Routes to appropriate model
      * forward_oauth - Forwards OAuth tokens to provider APIs
      * extract_session_id - Extracts session identifiers
      * capture_headers - Captures HTTP headers with sensitive redaction
      * forward_apikey - Forwards x-api-key header
      * add_beta_headers - Adds anthropic-beta headers for Claude Code OAuth
      * inject_claude_code_identity - Injects required system message for OAuth
    - cli.py: Tyro-based CLI interface for managing the proxy server
    - utils.py: Template discovery and debug utilities

    Configuration Files:
    - ~/.ccproxy/config.yaml - LiteLLM proxy configuration with model definitions
    - ~/.ccproxy/ccproxy.yaml - ccproxy-specific configuration (rules, hooks, debug)
    - ~/.ccproxy/ccproxy.py - Auto-generated handler file

    The rule system evaluates rules in order from ccproxy.yaml. Each rule inherits
    from ClassificationRule and implements evaluate(request, config) -> bool.
    First matching rule's name becomes the routing label.

    OAuth token refresh has two triggers:
    - TTL-based: Background task checks every 30 minutes, refreshes at 90% of oauth_ttl
    - 401-triggered: Immediate refresh when API returns authentication error

    Request metadata is stored by litellm_call_id with 60-second TTL auto-cleanup
    since LiteLLM doesn't preserve custom metadata.

    The project uses pytest with comprehensive fixtures (18 test files, 90% coverage).
    Singleton patterns (CCProxyConfig, ModelRouter) use clear_config_instance() and
    clear_router() to reset state in tests.
    """

    prompt = f"""
    {context}

    Based on this architecture description, please:
    1. List the files in the current directory
    2. Identify which component would handle OAuth token refresh
    3. Explain the role of the rule evaluation system

    Please be concise in your response.
    """

    # Configure Agent SDK options
    options = ClaudeAgentOptions(
        allowed_tools=["Read", "Glob"],
        permission_mode="default",  # Require permission for file operations
        cwd=os.getcwd(),
    )

    console.print(
        Panel.fit(
            "[cyan]Starting Agent SDK query with caching context...[/cyan]\n"
            f"[dim]Base URL: {os.environ['ANTHROPIC_BASE_URL']}[/dim]",
            title="Agent SDK Caching Example",
        )
    )

    # Execute query and collect messages
    messages_received = 0
    assistant_texts: list[str] = []
    final_usage: dict | None = None

    try:
        async for message in query(prompt=prompt, options=options):
            messages_received += 1

            if isinstance(message, AssistantMessage):
                console.print(f"\n[bold green]Assistant Message (Model: {message.model}):[/bold green]")
                for block in message.content:
                    if isinstance(block, TextBlock):
                        console.print(block.text)
                        assistant_texts.append(block.text)

            elif isinstance(message, ResultMessage):
                console.print(f"\n[bold blue]Result Message:[/bold blue]")
                console.print(f"  Subtype: {message.subtype}")
                console.print(f"  Duration: {message.duration_ms}ms (API: {message.duration_api_ms}ms)")
                console.print(f"  Turns: {message.num_turns}")
                console.print(f"  Session ID: {message.session_id}")
                console.print(f"  Error: {message.is_error}")

                if message.total_cost_usd is not None:
                    console.print(f"  Total Cost: ${message.total_cost_usd:.6f}")

                if message.usage:
                    final_usage = message.usage
                    console.print("\n[bold yellow]Usage Statistics:[/bold yellow]")

                    # Create usage table
                    table = Table(title="Token Usage", show_header=True)
                    table.add_column("Metric", style="cyan")
                    table.add_column("Value", style="green", justify="right")

                    for key, value in sorted(message.usage.items()):
                        # Highlight cache-related metrics
                        style = "bold yellow" if "cache" in key.lower() else "green"
                        table.add_row(key, str(value), style=style)

                    console.print(table)

                    # Display cache effectiveness
                    if "cache_read_input_tokens" in message.usage:
                        cache_reads = message.usage["cache_read_input_tokens"]
                        if cache_reads > 0:
                            console.print(
                                f"\n[bold green]✓ Cache Hit![/bold green] "
                                f"Read {cache_reads} tokens from cache"
                            )
                    elif "cache_creation_input_tokens" in message.usage:
                        cache_created = message.usage["cache_creation_input_tokens"]
                        console.print(
                            f"\n[bold cyan]Cache Created:[/bold cyan] "
                            f"{cache_created} tokens cached for future requests"
                        )

    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}", style="red")
        raise

    # Summary
    summary_text = (
        f"[green]Completed successfully[/green]\n"
        f"Messages received: {messages_received}\n"
        f"Assistant responses: {len(assistant_texts)}"
    )
    if final_usage:
        input_tokens = final_usage.get("input_tokens", 0)
        output_tokens = final_usage.get("output_tokens", 0)
        summary_text += f"\nTokens - Input: {input_tokens}, Output: {output_tokens}"

    console.print(Panel.fit(summary_text, title="Summary"))

    console.print(
        "\n[dim]Tip: Run this example multiple times to observe cache hit behavior.\n"
        "Check ccproxy logs for detailed cache metrics.[/dim]"
    )


if __name__ == "__main__":
    asyncio.run(main())
