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

restart:
    process-compose down
    process-compose up --detached

logs *ARGS:
    process-compose process logs ccproxy {{ARGS}}

# Build wheel for pip-install validation (mirrors the GHA build-wheel job)
build-wheel:
    rm -rf dist
    uv build --wheel

# Release-gate: boot a vanilla cloud VM and validate the install end-to-end.
# Pre-req: `just build-wheel`.
#
# Usage: just release-test-qemu debian-12 | ubuntu-24.04 | fedora-44
release-test-qemu DISTRO="debian-12":
    test -d dist || just build-wheel
    scripts/qemu_release_test.sh {{DISTRO}}

# Run release-gate test against every supported distro sequentially.
release-test-qemu-all:
    just build-wheel
    scripts/qemu_release_test.sh debian-12
    scripts/qemu_release_test.sh ubuntu-24.04
    scripts/qemu_release_test.sh fedora-44
