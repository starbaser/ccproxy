# Contributing to `ccproxy`

Thank you for your interest in contributing to `ccproxy`! As a brand new project, I welcome all forms of contributions.

## How to Contribute

### Reporting Issues

- **Questions & Discussions**: Open an issue for any questions or to start a discussion
- **Bug Reports**: Include steps to reproduce, expected vs actual behavior, and your environment details
- **Feature Requests**: Describe the feature and why it would be useful

### Code Contributions

1. **Fork the repository**
2. **Create a feature branch**: `git checkout -b feature/your-feature-name`
3. **Make your changes**
4. **Run tests**: `uv run pytest`
5. **Check types**: `uv run mypy src/ccproxy --strict`
6. **Format code**: `uv run ruff format src/ tests/`
7. **Lint code**: `uv run ruff check src/ tests/ --fix`
8. **Commit changes**: Use clear, descriptive commit messages
9. **Push to your fork**: `git push origin feature/your-feature-name`
10. **Open a Pull Request**

### Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/ccproxy.git
cd ccproxy

# Install development dependencies
uv sync

# Install pre-commit hooks
uv run pre-commit install

# Run tests to verify setup
uv run pytest
```

### Running `ccproxy` During Development

**Important**: When developing `ccproxy`, you must use `uv run` to ensure the local development version is used instead of any globally installed version:

```bash
# Run ccproxy commands with uv run
uv run ccproxy install
uv run ccproxy start

# Run litellm with the local ccproxy
cd ~/.ccproxy
uv run -m litellm --config config.yaml

# Or from the project directory
uv run litellm --config ~/.ccproxy/config.yaml
```

Without `uv run`, you may encounter import errors like "Could not import handler" because Python will try to use a globally installed version instead of your development code.

### Code Style

- **Type hints**: All functions must have complete type annotations
- **Testing**: Maintain >90% test coverage
- **Async**: Use async/await for all I/O operations
- **Error handling**: All hooks must handle errors gracefully
- **Documentation**: Code should be self-documenting through clear naming

### Testing

- Write tests for all new functionality
- Test edge cases and error conditions
- Run the full test suite before submitting: `uv run pytest tests/ -v --cov=ccproxy --cov-report=term-missing`

**E2E Tests**: The test suite includes end-to-end tests that run the real Claude CLI. These tests require:
- Claude Code CLI installed and available in PATH
- A logged-in Claude subscription with valid OAuth credentials (`~/.claude/.credentials.json`)

To skip E2E tests: `uv run pytest -m "not e2e"`

### Pull Request Guidelines

- **One feature per PR**: Keep PRs focused on a single change
- **Clear description**: Explain what changes you made and why
- **Link issues**: Reference any related issues
- **Tests pass**: All tests and checks must pass
- **Documentation**: Update docs if you change functionality

## Getting Help

- Open an issue for questions
- Check existing issues for similar problems
- Join discussions in issue threads

## Code of Conduct

Be respectful and constructive in all interactions. We're all here to build something useful together.

## License

By contributing, you agree that your contributions will be licensed under the same license as the project (see LICENSE file).
