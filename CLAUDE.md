# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

`ccproxy` is a command-line tool that intercepts and routes Claude Code's requests to different LLM providers via a LiteLLM proxy server. It enables intelligent request routing based on token count, model type, tool usage, or custom rules.

## Development Commands

### Running Tests

```bash
# Run all tests with coverage
uv run pytest

# Run specific test file
uv run pytest tests/test_classifier.py

# Run tests matching pattern
uv run pytest -k "test_token_count"

# Run with verbose output
uv run pytest -v
```

### Linting & Formatting

```bash
# Format code with ruff
uv run ruff format .

# Check linting issues
uv run ruff check .

# Fix linting issues automatically
uv run ruff check --fix .

# Type checking with mypy
uv run mypy src/ccproxy
```

### Development Setup

```bash
# Install with dev dependencies
uv sync --dev

# Install as a tool globally
uv tool install .

# Run the module directly
uv run python -m ccproxy
```

### CLI Commands

```bash
# Install configuration files
uv run ccproxy install [--force]

# Start/stop proxy server
uv run ccproxy start [--detach]
uv run ccproxy stop

# View logs
uv run ccproxy logs [-f] [-n LINES]

# Run command with proxy environment
uv run ccproxy run <command> [args...]
```

## Architecture

The codebase follows a modular architecture with clear separation of concerns:

### Request Flow

1. **CCProxyHandler** (`handler.py`) - LiteLLM CustomLogger that intercepts all requests
2. **RequestClassifier** (`classifier.py`) - Evaluates rules to determine routing
3. **ModelRouter** (`router.py`) - Maps rule names to actual model configurations
4. **User Hooks** - Optional Python functions that can modify requests/responses

### Key Components

- **handler.py**: Main entry point as a LiteLLM CustomLogger. Orchestrates the classification and routing process.
- **classifier.py**: Rule-based classification system that evaluates rules in order to determine routing.
- **rules.py**: Defines `ClassificationRule` abstract base class and built-in rules (TokenCountRule, MatchModelRule, ThinkingRule, MatchToolRule).
- **router.py**: Manages model configurations from LiteLLM proxy server and provides fallback logic.
- **config.py**: Configuration management using Pydantic, loads from `ccproxy.yaml`.
- **hooks.py**: Built-in hooks (rule_evaluator, model_router, forward_oauth) that process requests.
- **cli.py**: Tyro-based CLI interface for managing the proxy server.

### Rule System

Rules are evaluated in the order configured in `ccproxy.yaml`. Each rule:

- Inherits from `ClassificationRule` abstract base class
- Implements `evaluate(request, config) -> bool` method
- Returns the first matching rule's name as the routing label

Custom rules can be created by implementing the ClassificationRule interface and specifying the Python import path in the configuration.

### Configuration Files

- `~/.ccproxy/config.yaml` - LiteLLM proxy configuration with model definitions
- `~/.ccproxy/ccproxy.yaml` - ccproxy-specific configuration (rules, hooks, debug settings)
- `~/.ccproxy/ccproxy.py` - Optional user hooks for custom request/response processing

## Testing Patterns

The test suite uses pytest with comprehensive fixtures:

- `mock_proxy_server` fixture for mocking LiteLLM proxy
- `cleanup` fixture ensures singleton instances are cleared between tests
- Tests are organized to mirror source structure (`test_<module>.py`)
- Integration tests verify end-to-end behavior
- Edge case tests ensure robustness

## Important Implementation Notes

The project uses singleton patterns for `CCProxyConfig` and `ModelRouter` - use `clear_config_instance()` and `clear_router()` to reset state in tests

- Token counting uses tiktoken with fallback to character-based estimation
- OAuth token forwarding is handled specially for Claude CLI requests to Anthropic API
- Rules can accept parameters via the `params` field in configuration
- The handler processes multiple hooks in sequence with error isolation

## Cache Analysis Tools

The `scripts/` directory contains cache analysis tools for optimizing Claude Code's caching:

- `cache_analyzer.py` - Reverse proxy that analyzes cache patterns
- Dashboard on port 5555 shows real-time cache metrics
- Identifies opportunities for 1-hour cache optimization

## Dependencies

Key dependencies include:

- **litellm[proxy]** - Core proxy functionality
- **pydantic** - Configuration and validation
- **tyro** - CLI interface
- **tiktoken** - Token counting
- **anthropic** - Anthropic API client
- **rich** - Terminal output formatting
