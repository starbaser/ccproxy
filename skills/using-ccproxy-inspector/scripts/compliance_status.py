#!/usr/bin/env python3
"""Show compliance profile status and contents.

Reads the compliance profiles JSON directly and displays profile
summaries, accumulator progress, and detailed profile contents.

Usage:
    uv run python scripts/compliance_status.py
    uv run python scripts/compliance_status.py --provider anthropic
    uv run python scripts/compliance_status.py --seed-status
    uv run python scripts/compliance_status.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _resolve_store_path() -> Path:
    env_dir = os.environ.get("CCPROXY_CONFIG_DIR")
    config_dir = Path(env_dir) if env_dir else Path.home() / ".ccproxy"
    return config_dir / "compliance_profiles.json"


def _load_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"format_version": 1, "profiles": {}, "accumulators": {}}
    try:
        data = json.loads(path.read_text())
        if data.get("format_version") != 1:
            print(f"Warning: Unknown format version {data.get('format_version')}", file=sys.stderr)
        return data
    except (json.JSONDecodeError, KeyError) as e:
        print(f"Error: Malformed compliance profiles: {e}", file=sys.stderr)
        sys.exit(1)


def _get_min_observations() -> int:
    try:
        from ccproxy.config import get_config

        return get_config().compliance.min_observations
    except Exception:
        return 3


def _profile_summary(key: str, profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": key,
        "provider": profile["provider"],
        "user_agent": profile["user_agent"],
        "observation_count": profile["observation_count"],
        "is_complete": profile["is_complete"],
        "num_headers": len(profile.get("headers", [])),
        "num_body_fields": len(profile.get("body_fields", [])),
        "has_system": profile.get("system") is not None,
        "has_body_wrapper": profile.get("body_wrapper") is not None,
        "body_wrapper": profile.get("body_wrapper"),
        "updated_at": profile.get("updated_at", ""),
        "is_seed": profile.get("user_agent") == "v0-seed" and profile.get("observation_count", 0) == 0,
    }


def _accumulator_summary(key: str, acc: dict[str, Any], min_obs: int) -> dict[str, Any]:
    count = acc.get("observation_count", 0)
    remaining = max(0, min_obs - count)
    pct = min(100.0, (count / min_obs * 100)) if min_obs > 0 else 100.0
    return {
        "key": key,
        "provider": acc["provider"],
        "user_agent": acc["user_agent"],
        "observation_count": count,
        "observations_needed": min_obs,
        "remaining": remaining,
        "progress_pct": round(pct, 1),
    }


def _profile_detail(profile: dict[str, Any]) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "provider": profile["provider"],
        "user_agent": profile["user_agent"],
        "observation_count": profile["observation_count"],
        "created_at": profile.get("created_at"),
        "updated_at": profile.get("updated_at"),
    }

    detail["headers"] = [
        {"name": h["name"], "value": h["value"]}
        for h in profile.get("headers", [])
    ]

    detail["body_fields"] = [
        {"path": f["path"], "value": f["value"]}
        for f in profile.get("body_fields", [])
    ]

    if profile.get("system"):
        detail["system"] = profile["system"]

    if profile.get("body_wrapper"):
        detail["body_wrapper"] = profile["body_wrapper"]

    return detail


def _print_rich(
    profiles: list[dict[str, Any]],
    accumulators: list[dict[str, Any]],
    detail: dict[str, Any] | None,
    seed_status: dict[str, Any] | None,
) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    console = Console()

    # Profiles table
    if profiles:
        table = Table(title="Compliance Profiles", show_header=True, header_style="bold")
        table.add_column("Provider", style="cyan")
        table.add_column("User Agent", max_width=40)
        table.add_column("Obs", justify="right")
        table.add_column("Headers", justify="right")
        table.add_column("Body", justify="right")
        table.add_column("System", width=7)
        table.add_column("Wrapper", width=10)
        table.add_column("Seed", width=5)
        table.add_column("Updated")

        for p in profiles:
            sys_str = "[green]yes[/green]" if p["has_system"] else "[dim]-[/dim]"
            wrap_str = p["body_wrapper"] if p["has_body_wrapper"] else "[dim]-[/dim]"
            seed_str = "[yellow]seed[/yellow]" if p["is_seed"] else "[dim]-[/dim]"
            table.add_row(
                p["provider"],
                p["user_agent"][:40],
                str(p["observation_count"]),
                str(p["num_headers"]),
                str(p["num_body_fields"]),
                sys_str,
                wrap_str,
                seed_str,
                p["updated_at"][:19] if p["updated_at"] else "-",
            )
        console.print(table)
    else:
        console.print("[dim]No compliance profiles.[/dim]")

    # Accumulators table
    if accumulators:
        table = Table(title="Accumulator Progress", show_header=True, header_style="bold")
        table.add_column("Provider", style="cyan")
        table.add_column("User Agent", max_width=40)
        table.add_column("Observations", justify="right")
        table.add_column("Needed", justify="right")
        table.add_column("Remaining", justify="right")
        table.add_column("Progress")

        for a in accumulators:
            pct = a["progress_pct"]
            bar_len = int(pct / 5)
            bar = "[green]" + "=" * bar_len + "[/green]" + "[dim]" + "-" * (20 - bar_len) + "[/dim]"
            table.add_row(
                a["provider"],
                a["user_agent"][:40],
                str(a["observation_count"]),
                str(a["observations_needed"]),
                str(a["remaining"]),
                f"{bar} {pct}%",
            )
        console.print(table)

    # Detail view
    if detail:
        parts = [f"Provider: {detail['provider']}", f"User Agent: {detail['user_agent']}"]
        parts.append(f"Observations: {detail['observation_count']}")
        parts.append("")

        if detail.get("headers"):
            parts.append("Headers:")
            for h in detail["headers"]:
                parts.append(f"  {h['name']}: {h['value']}")
            parts.append("")

        if detail.get("body_fields"):
            parts.append("Body Fields:")
            for f in detail["body_fields"]:
                val = json.dumps(f["value"]) if isinstance(f["value"], (dict, list)) else str(f["value"])
                parts.append(f"  {f['path']}: {val[:100]}")
            parts.append("")

        if detail.get("system"):
            parts.append("System Prompt Structure:")
            parts.append(f"  {json.dumps(detail['system'], indent=2)[:500]}")
            parts.append("")

        if detail.get("body_wrapper"):
            parts.append(f"Body Wrapper: {detail['body_wrapper']}")

        console.print(Panel("\n".join(parts), title="Profile Detail"))

    # Seed status
    if seed_status:
        if seed_status["active"]:
            console.print(
                f"[yellow]Anthropic v0 seed is ACTIVE[/yellow] — no learned profile has superseded it yet. "
                f"Run Claude Code through WireGuard ({seed_status['remaining']} more observations needed)."
            )
        else:
            console.print(
                f"[green]Anthropic v0 seed is SUPERSEDED[/green] by learned profile "
                f"(ua={seed_status['learned_ua'][:40]}, {seed_status['learned_obs']} observations)"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Show ccproxy compliance profile status")
    parser.add_argument("--provider", help="Show detail for a specific provider")
    parser.add_argument("--seed-status", action="store_true", help="Show Anthropic v0 seed status")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    store_path = _resolve_store_path()
    data = _load_store(store_path)
    min_obs = _get_min_observations()

    profiles = [
        _profile_summary(k, p) for k, p in data.get("profiles", {}).items()
    ]
    accumulators = [
        _accumulator_summary(k, a, min_obs) for k, a in data.get("accumulators", {}).items()
    ]

    # Detail for --provider
    detail: dict[str, Any] | None = None
    if args.provider:
        for p in data.get("profiles", {}).values():
            if p["provider"] == args.provider and p.get("is_complete"):
                detail = _profile_detail(p)
                break

    # Seed status
    seed_status: dict[str, Any] | None = None
    if args.seed_status:
        seed_profile = None
        learned_profile = None
        for p in data.get("profiles", {}).values():
            if p["provider"] != "anthropic":
                continue
            if p.get("user_agent") == "v0-seed":
                seed_profile = p
            elif (
                p.get("is_complete")
                and p.get("observation_count", 0) > 0
                and (learned_profile is None or p.get("updated_at", "") > learned_profile.get("updated_at", ""))
            ):
                learned_profile = p

        # Check accumulator progress
        acc_remaining = min_obs
        for a in data.get("accumulators", {}).values():
            if a["provider"] == "anthropic":
                acc_remaining = max(0, min_obs - a.get("observation_count", 0))

        seed_status = {
            "seed_exists": seed_profile is not None,
            "active": learned_profile is None,
            "remaining": acc_remaining,
            "learned_ua": learned_profile.get("user_agent", "") if learned_profile else "",
            "learned_obs": learned_profile.get("observation_count", 0) if learned_profile else 0,
        }

    if args.json:
        output = {
            "store_path": str(store_path),
            "store_exists": store_path.exists(),
            "min_observations": min_obs,
            "profiles": profiles,
            "accumulators": accumulators,
        }
        if detail:
            output["detail"] = detail
        if seed_status:
            output["seed_status"] = seed_status
        json.dump(output, sys.stdout, indent=2, default=str)
        print()
    else:
        _print_rich(profiles, accumulators, detail, seed_status)


if __name__ == "__main__":
    main()
