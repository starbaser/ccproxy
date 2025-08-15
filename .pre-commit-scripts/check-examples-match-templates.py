#!/usr/bin/env python3
"""Pre-commit hook to verify example files match template files."""

import sys
from pathlib import Path


def check_file_match(example_path: Path, template_path: Path) -> bool:
    """Check if two files have identical content.

    Args:
        example_path: Path to example file
        template_path: Path to template file

    Returns:
        True if files match, False otherwise
    """
    if not example_path.exists():
        print(f"❌ Example file not found: {example_path}")
        return False

    if not template_path.exists():
        print(f"❌ Template file not found: {template_path}")
        return False

    example_content = example_path.read_bytes()
    template_content = template_path.read_bytes()

    if example_content != template_content:
        print(f"❌ Content mismatch: {example_path} != {template_path}")
        print(f"   Run: cp {template_path} {example_path}")
        return False

    return True


def main() -> int:
    """Main entry point for the pre-commit hook.

    Returns:
        0 if all files match, 1 if any mismatch found
    """
    # Define file pairs to check
    file_pairs = [
        ("examples/ccproxy.py", "src/ccproxy/templates/ccproxy.py"),
        ("examples/ccproxy.yaml", "src/ccproxy/templates/ccproxy.yaml"),
        ("examples/config.yaml", "src/ccproxy/templates/config.yaml"),
    ]

    # Get repository root
    repo_root = Path(__file__).parent.parent

    all_match = True
    for example_rel, template_rel in file_pairs:
        example_path = repo_root / example_rel
        template_path = repo_root / template_rel

        if not check_file_match(example_path, template_path):
            all_match = False

    if all_match:
        print("✅ All example files match their templates")
        return 0
    else:
        print("\n⚠️  Example files do not match templates!")
        print("   To fix: Copy template files to examples directory")
        return 1


if __name__ == "__main__":
    sys.exit(main())
