# Development

export PC_SOCKET_PATH := "/tmp/process-compose-ccproxy.sock"

test:
    uv run pytest

lint:
    uv run ruff check .

fmt:
    uv run ruff format .

typecheck:
    uv run mypy src/ccproxy

# Process management
up:
    process-compose up --detached

down:
    process-compose down
