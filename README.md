# `ccproxy` - Claude Code Proxy

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/starbased-co/ccproxy)

`ccproxy` is a command-line tool designed for Claude Code that intercepts, inspects, modifies, and redirects Claude Code's requests made to Anthropic's Messages API to any LLM provider. To accomplish this, `ccproxy` starts a [LiteLLM Proxy Server](https://docs.litellm.ai/docs/simple_proxy) as a background process, configures the needed environment for `claude` to run as a transient child process (`ccproxy run claude`), and enables you to intelligently decide how and where each and every model request is made using either our pre-configured routing rules, your own rules using the custom plugin's framework, or whatever code you want through configurable user-hooks.

## Key Features

- **Claude MAX Plan Integration**: Seamlessly use your unlimited Claude MAX (and Pro) subscription.

- **Intelligent Request Routing**: Automatically route requests based on token count, model type, tool usage, or custom rules - send large contexts to Gemini, web searches to Perplexity, and keep standard requests on Claude

- **Custom Rule Framework**: Create your own Python-based routing rules with full access to request properties, conversation context, and dynamic parameters

- **User Hooks**: Intercept and modify requests/responses at any stage with configurable pre/post-call hooks for complete control over the API flow

- **Full LiteLLM Proxy Features**: Built on LiteLLM, includes load balancing, automatic fallbacks, spend tracking, rate limiting, caching, and 100+ provider support out of the box

- **Cross-Provider Context Preservation** _(coming soon)_: Maintain conversation history and context when routing between different models and providers.

> ⚠️ **Note**: This is a newly released project ready for public use and feedback. While core functionality is complete, real-world testing and community input are welcomed. Please [open an issue](https://github.com/starbased-co/ccproxy/issues) to share your experience, report bugs, or suggest improvements.

> **Known Issue**: Context preservation between providers has not yet been fully implemented. Due to the way how cache breakpoints work, routing requests in-between different models/providers will result in lowered cache efficiency. Improving this is the next major feature being worked on.

## Installation

```bash
# Recommended: install as a uv tool
uv tool install git+https://github.com/starbased-co/ccproxy.git
# or
pipx install git+https://github.com/starbased-co/ccproxy.git

# Alternative: Install with pip
pip install git+https://github.com/starbased-co/ccproxy.git
```

## Quick Setup

Run the automated setup:

```bash
ccproxy install
# or with Python module:
python -m ccproxy install
```

This will create all necessary configuration files in `~/.ccproxy/`.

To overwrite existing files without prompting:

```bash
ccproxy install --force
```

See [docs/configuration.md](docs/configuration.md).

### Routing Rules

`ccproxy` includes built-in rules for intelligent request routing:

- **TokenCountRule**: Routes requests with large token counts to high-capacity models
- **MatchModelRule**: Routes based on the requested model name
- **ThinkingRule**: Routes requests containing a "thinking" field
- **MatchToolRule**: Routes based on tool usage (e.g., WebSearch)

You can also create custom rules - see the examples directory for details. Custom rules (and hooks) are loaded with the same mechanism that LiteLLM uses to import the custom callbacks, that is, they are imported as by the LiteLLM python process as named module from within it's virtual environment (e.g. `import custom_rule_file.custom_rule_function`), or as a python script adjacent to `config.yaml`.

#### Example Configuration

```yaml
# LiteLLM model configuration
model_list:
  # Default model for regular use
  - model_name: default
    litellm_params:
      model: claude-sonnet-4-20250514

  # Background model for low-cost operations
  - model_name: background
    litellm_params:
      model: claude-3-5-haiku-20241022

  # Thinking model for complex reasoning
  - model_name: think
    litellm_params:
      model: claude-opus-4-20250514

  # Large context model for >60k tokens
  - model_name: token_count
    litellm_params:
      model: gemini-2.5-pro

  # Web search model for tool usage
  - model_name: web_search
    litellm_params:
      model: gemini-2.5-flash

  # Anthropic provided claude models, no `api_key` needed
  - model_name: claude-sonnet-4-20250514
    litellm_params:
      model: anthropic/claude-3-5-sonnet-20241022
      api_base: https://api.anthropic.com

  - model_name: claude-opus-4-20250514
    litellm_params:
      model: anthropic/claude-opus-4-20250514
      api_base: https://api.anthropic.com

  - model_name: claude-3-5-haiku-20241022
    litellm_params:
      model: anthropic/claude-3-5-haiku-20241022
      api_base: https://api.anthropic.com

  # Add any other provider/model supported by LiteLLM

  - model_name: gemini-2.5-pro
    litellm_params:
      model: gemini/gemini-2.5-pro
      api_base: https://generativelanguage.googleapis.com
      api_key: os.environ/GOOGLE_API_KEY
```

## CLI Commands

`ccproxy` provides several commands for managing the proxy server:

```bash
# Install configuration files
ccproxy install [--force]

# Start LiteLLM
ccproxy start [--detach]

# Stop LiteLLM
ccproxy stop

# View proxy server logs
ccproxy logs [-f] [-n LINES]

# Run any command with proxy environment variables
ccproxy run <command> [args...]

```

## Usage

After installation and setup, you can run any command through the ccproxy:

```bash
# Run Claude Code through the proxy
ccproxy run claude --version
ccproxy run claude -p "Explain quantum computing"

# Run other tools through the proxy
ccproxy run curl http://localhost:4000/health
ccproxy run python my_script.py

```

The `ccproxy run` command sets up the following environment variables:

- `ANTHROPIC_BASE_URL` - For Anthropic SDK compatibility
- `OPENAI_API_BASE` - For OpenAI SDK compatibility
- `OPENAI_BASE_URL` - For OpenAI SDK compatibility

**Note**: Using `ccproxy run` is not required. You can also simply export `ANTHROPIC_BASE_URL` to point to your LiteLLM server:

```bash
ccproxy start
export ANTHROPIC_BASE_URL=http://localhost:4000 # Add to your .zshrc/.bashrc
claude -p "Explain quantum computing"
```

For detailed configuration documentation including custom rules and advanced usage, see [docs/configuration.md](docs/configuration.md).

## Contributing

I welcome contributions! Please see the [Contributing Guide](CONTRIBUTING.md) for details on:

- Reporting issues and asking questions
- Setting up development environment
- Code style and testing requirements
- Submitting pull requests

Since this is a new project, I especially appreciate:

- Bug reports and feedback
- Documentation improvements
- Test coverage additions
- Feature suggestions
- Any of your implementations using `ccproxy`

## Acknowledgments

Inspired in part by [claude-code-router](https://github.com/musistudio/claude-code-router).
