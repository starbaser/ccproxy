# Development

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

logs *ARGS:
    process-compose process logs ccproxy {{ARGS}}

# Regenerate src/ccproxy/templates/ccproxy.yaml from nix/defaults.nix
sync-template:
    nix eval --json .#defaultSettings.settings | python3 scripts/render_template.py > src/ccproxy/templates/ccproxy.yaml
