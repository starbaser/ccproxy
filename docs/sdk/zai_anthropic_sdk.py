#!/usr/bin/env python3
"""Example using Anthropic SDK with Z.AI GLM models via ccproxy.

Demonstrates routing GLM-4.7 requests through ccproxy with prompt caching.
The proxy handles authentication via ZAI_API_KEY configured in ~/.ccproxy/config.yaml.

Requirements:
- ccproxy running: `ccproxy start --detach`
- ZAI_API_KEY configured in environment (for config.yaml)
- glm-4.7 model defined in ~/.ccproxy/config.yaml

Prompt Caching:
- Z.AI accepts cache_control in requests but may not create/read cache entries
- The anthropic-beta header is forwarded: "prompt-caching-2024-07-31"
- Use cache_control={"type": "ephemeral"} on system prompts (1024+ tokens)
- Response includes cache_read_input_tokens field (may be 0 if caching not active)
- Note: Z.AI caching behavior differs from native Anthropic API
"""

import anthropic
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()
err_console = Console(stderr=True)

# Large system prompt (1024+ tokens required for caching)
# This prompt is intentionally verbose to exceed the minimum token threshold
CACHED_SYSTEM_PROMPT = """You are a helpful coding assistant with deep expertise in Python development.
You provide clear, well-structured code with comprehensive explanations.

## Core Principles

### Code Quality Standards
1. Write clean, readable code with meaningful variable names that convey intent and purpose
2. Include comprehensive type hints for all function parameters, return values, and class attributes
3. Add detailed docstrings to functions, classes, and modules following Google style guide format
4. Handle errors gracefully with appropriate exception handling and custom exception hierarchies
5. Follow PEP 8 style guidelines strictly, using automated tools like ruff or black for enforcement
6. Prefer composition over inheritance for flexible, maintainable, and testable designs
7. Write testable code using dependency injection, interface segregation, and single responsibility
8. Use context managers for proper resource management including files, connections, and locks
9. Leverage Python's standard library before reaching for external dependencies
10. Document edge cases, assumptions, non-obvious behavior, and performance characteristics

### Security Best Practices and Vulnerability Prevention
When reviewing or writing code, always check for and prevent these security issues:
- SQL injection vulnerabilities: Always use parameterized queries, never use string formatting
- Command injection: Avoid shell=True in subprocess, use argument lists instead of shell strings
- XSS vulnerabilities: Escape all user input in templates, use safe serialization methods
- Path traversal attacks: Validate and sanitize all file paths, use pathlib for path manipulation
- Sensitive data exposure: Never log secrets or credentials, use environment variables or vaults
- Authentication flaws: Implement proper session management, use bcrypt or argon2 for passwords
- CSRF protection: Use tokens for all state-changing operations, validate origin headers
- Insecure deserialization: Avoid pickle for untrusted data, prefer JSON with schema validation
- Broken access control: Implement principle of least privilege, validate permissions on every request
- Security misconfiguration: Use secure defaults, disable debug mode in production environments

### Performance Optimization Strategies
Consider these performance aspects when designing and implementing solutions:
- Time complexity: Prefer O(n) or O(log n) algorithms when possible, avoid O(n²) nested loops
- Space complexity: Be mindful of memory usage with large datasets, use streaming when appropriate
- I/O bottlenecks: Use async/await for I/O-bound operations, implement connection pooling
- CPU bottlenecks: Consider multiprocessing for CPU-bound work, use numpy for numerical operations
- Caching strategies: Implement appropriate caching with functools.lru_cache, Redis, or memcached
- Database queries: Avoid N+1 problems with eager loading, use proper indexing and batch operations
- Memory leaks: Clean up resources properly, avoid circular references, use weak references
- Lazy evaluation: Use generators for large sequences, leverage itertools for memory efficiency
- Profiling: Use cProfile, line_profiler, and memory_profiler to identify actual bottlenecks

### Testing Standards and Quality Assurance
- Write unit tests with pytest, aiming for greater than 80% code coverage on business logic
- Use fixtures for test setup and teardown, leverage conftest.py for shared fixtures
- Mock external dependencies with unittest.mock or pytest-mock to isolate units under test
- Write integration tests for critical paths and API endpoints with realistic test data
- Use property-based testing with hypothesis for edge cases and invariant validation
- Implement contract tests for API boundaries between services and external systems
- Run tests in CI/CD pipeline with GitHub Actions, GitLab CI, or similar automation tools
- Include performance tests and benchmarks for latency-sensitive code paths

### Documentation Requirements and Standards
- README with clear setup instructions, usage examples, and troubleshooting guides
- API documentation with type hints, docstrings, and example requests/responses
- Architecture decision records (ADRs) for significant technical choices and trade-offs
- Changelog following Keep a Changelog format with semantic versioning
- Contributing guidelines for open source projects including code style and PR process
- Inline comments for complex algorithms explaining the why, not just the what

### Python-Specific Patterns and Idioms
- Use dataclasses or attrs for data containers with automatic __init__, __repr__, and __eq__
- Implement __slots__ for memory-efficient classes when you have many instances
- Use typing.Protocol for structural subtyping and duck typing with static type checking
- Leverage functools for decorators, partial application, and higher-order functions
- Use contextlib for custom context managers with @contextmanager decorator
- Implement __enter__/__exit__ or async variants __aenter__/__aexit__ properly for resources
- Use enum.Enum for type-safe constants with automatic value generation and iteration
- Apply the descriptor protocol for reusable property logic and attribute access control
- Use __init_subclass__ for class registration and validation patterns

### Async Programming Best Practices
- Use asyncio for concurrent I/O operations with proper event loop management
- Implement proper cancellation handling with asyncio.shield for critical sections
- Use aiohttp or httpx for async HTTP clients with connection pooling and timeouts
- Implement connection pooling for database connections with asyncpg or databases library
- Handle backpressure with bounded queues using asyncio.Queue with maxsize parameter
- Use asyncio.gather for parallel coroutines with return_exceptions for error handling
- Implement proper cleanup with async context managers and asyncio.TaskGroup
- Avoid blocking calls in async code, use run_in_executor for CPU-bound operations

### Error Handling Patterns and Best Practices
- Create custom exception hierarchies for domain errors with meaningful error messages
- Use exception chaining with 'from' for wrapped errors to preserve original traceback
- Implement retry logic with exponential backoff and jitter for transient failures
- Log errors with proper context, stack traces, and correlation IDs for debugging
- Return Result types for expected failures using libraries like returns or result
- Use warnings module for deprecation notices and non-fatal issues
- Implement circuit breakers for external service calls to prevent cascade failures
- Distinguish between recoverable and non-recoverable errors in exception handling

Remember: Code is read far more often than it is written. Always prioritize clarity,
maintainability, and correctness over cleverness or premature optimization.
"""


# Beta header required for prompt caching
PROMPT_CACHING_BETA = "prompt-caching-2024-07-31"


def create_client(with_caching: bool = False) -> anthropic.Anthropic:
    """Create Anthropic client configured for ccproxy.

    Args:
        with_caching: Enable prompt caching beta header
    """
    default_headers = {}
    if with_caching:
        default_headers["anthropic-beta"] = PROMPT_CACHING_BETA

    return anthropic.Anthropic(
        api_key="sk-proxy-dummy",  # Dummy key - ccproxy handles real auth
        base_url="http://127.0.0.1:4000",
        default_headers=default_headers if default_headers else None,
    )


def get_text(response: anthropic.types.Message) -> str:
    """Extract text from response content blocks."""
    for block in response.content:
        if hasattr(block, "text"):
            return block.text  # type: ignore[return-value]
    return ""


def print_cache_stats(usage: anthropic.types.Usage) -> None:
    """Display cache statistics from response usage."""
    table = Table(title="Token Usage & Cache Stats", show_header=True)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green", justify="right")

    table.add_row("Input tokens", str(usage.input_tokens))
    table.add_row("Output tokens", str(usage.output_tokens))

    # Cache statistics (may be None if not supported)
    cache_read = getattr(usage, "cache_read_input_tokens", None)
    cache_creation = getattr(usage, "cache_creation_input_tokens", None)

    if cache_read is not None:
        table.add_row("Cache read tokens", str(cache_read))
    if cache_creation is not None:
        table.add_row("Cache creation tokens", str(cache_creation))

    # Calculate cache hit ratio if available
    if cache_read and usage.input_tokens > 0:
        hit_ratio = (cache_read / usage.input_tokens) * 100
        table.add_row("Cache hit ratio", f"{hit_ratio:.1f}%")

    console.print(table)


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
    console.print(get_text(response))
    console.print(f"\n[dim]Tokens: {response.usage.input_tokens} in, {response.usage.output_tokens} out[/dim]")


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


def cached_request_demo() -> None:
    """Demonstrate prompt caching with a large system prompt.

    Makes two requests with the same system prompt to show cache behavior:
    - First request: May create cache entry
    - Second request: Should read from cache

    Note: Requires anthropic-beta header for prompt caching to work.
    """
    console.print(Panel("[cyan]Prompt Caching Example[/cyan]", border_style="blue", subtitle="Two requests"))

    client = create_client(with_caching=True)

    # First request - may create cache
    console.print("[yellow]Request 1:[/yellow] Initial request (may create cache)")
    response1 = client.messages.create(
        model="glm-4.7",
        max_tokens=150,
        system=[
            {
                "type": "text",
                "text": CACHED_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},  # Enable caching
            }
        ],
        messages=[{"role": "user", "content": "Write a one-line Python function to check if a number is prime."}],
    )

    console.print(f"[green]Response:[/green] {get_text(response1)}\n")
    print_cache_stats(response1.usage)

    # Second request - should hit cache
    console.print("\n[yellow]Request 2:[/yellow] Follow-up request (should hit cache)")
    response2 = client.messages.create(
        model="glm-4.7",
        max_tokens=150,
        system=[
            {
                "type": "text",
                "text": CACHED_SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": "Now write a one-line function to check if a string is a palindrome."}],
    )

    console.print(f"[green]Response:[/green] {get_text(response2)}\n")
    print_cache_stats(response2.usage)

    # Compare cache stats
    cache1 = getattr(response1.usage, "cache_read_input_tokens", 0) or 0
    cache2 = getattr(response2.usage, "cache_read_input_tokens", 0) or 0

    if cache2 > cache1:
        console.print(
            f"\n[green]✓ Cache hit improved![/green] "
            f"Request 1: {cache1} tokens cached → Request 2: {cache2} tokens cached"
        )


def multi_turn_cached() -> None:
    """Multi-turn conversation with cached context."""
    console.print(Panel("[cyan]Multi-turn with Caching[/cyan]", border_style="blue"))

    client = create_client(with_caching=True)
    messages: list[anthropic.types.MessageParam] = []

    prompts = [
        "What's a generator in Python?",
        "Show a simple example.",
        "How does yield differ from return?",
    ]

    for i, prompt in enumerate(prompts, 1):
        console.print(f"\n[yellow]Turn {i}:[/yellow] {prompt}")

        messages.append({"role": "user", "content": prompt})

        response = client.messages.create(
            model="glm-4.7",
            max_tokens=200,
            system=[
                {
                    "type": "text",
                    "text": CACHED_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=messages,
        )

        assistant_text = get_text(response)
        console.print(f"[green]Response:[/green] {assistant_text[:200]}...")

        # Add assistant response to conversation
        messages.append({"role": "assistant", "content": assistant_text})

        # Show cache stats
        cache_read = getattr(response.usage, "cache_read_input_tokens", 0) or 0
        console.print(
            f"[dim]Tokens: {response.usage.input_tokens} in, "
            f"{response.usage.output_tokens} out, "
            f"{cache_read} cached[/dim]"
        )


def main() -> None:
    """Run examples."""
    try:
        console.print("[yellow]Note:[/yellow] Using GLM-4.7 via ccproxy\n")

        simple_request()
        console.print()

        streaming_request()

        cached_request_demo()
        console.print()

        multi_turn_cached()

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
