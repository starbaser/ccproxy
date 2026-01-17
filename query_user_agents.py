#!/usr/bin/env python3
"""Query User-Agent statistics from mitm postgres database."""

import asyncio
import json
from collections import Counter
from datetime import datetime

import asyncpg
from rich.console import Console
from rich.table import Table

console = Console()

# Database connection for MITM traces
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "ccproxy",
    "password": "test",
    "database": "ccproxy_mitm",  # MITM database, not litellm
}


async def get_user_agent_stats():
    """Query and display User-Agent statistics."""
    try:
        conn = await asyncpg.connect(**DB_CONFIG)
        console.print("[green]✓[/green] Connected to database")

        # Query all traces with their request headers
        query = """
            SELECT
                trace_id,
                request_headers,
                method,
                host,
                path,
                status_code,
                start_time,
                traffic_type
            FROM "CCProxy_HttpTraces"
            ORDER BY start_time DESC
        """

        rows = await conn.fetch(query)
        console.print(f"\n[cyan]Total traces:[/cyan] {len(rows)}")

        if not rows:
            console.print("[yellow]No traces found in database[/yellow]")
            await conn.close()
            return

        # Extract User-Agent from request_headers JSON
        user_agents = Counter()
        user_agent_details = []

        for row in rows:
            headers = row["request_headers"]
            if isinstance(headers, str):
                headers = json.loads(headers)

            # Headers can be in various formats, try common keys
            user_agent = None
            for key in ["User-Agent", "user-agent", "USER-AGENT"]:
                if key in headers:
                    user_agent = headers[key]
                    if isinstance(user_agent, list):
                        user_agent = user_agent[0] if user_agent else None
                    break

            if user_agent:
                user_agents[user_agent] += 1
                user_agent_details.append(
                    {
                        "user_agent": user_agent,
                        "method": row["method"],
                        "host": row["host"],
                        "path": row["path"],
                        "status": row["status_code"],
                        "time": row["start_time"],
                        "type": row["traffic_type"],
                    }
                )

        await conn.close()

        # Display statistics
        console.print(f"\n[cyan]Unique User-Agents:[/cyan] {len(user_agents)}")

        # Summary table
        table = Table(title="User-Agent Statistics", show_lines=True)
        table.add_column("User-Agent", style="cyan", no_wrap=False)
        table.add_column("Count", style="yellow", justify="right")
        table.add_column("Percentage", style="green", justify="right")

        total = sum(user_agents.values())
        for ua, count in user_agents.most_common():
            percentage = (count / total) * 100
            table.add_row(ua, str(count), f"{percentage:.1f}%")

        console.print("\n")
        console.print(table)

        # Recent traces with User-Agent
        console.print("\n[bold]Recent Traces (last 10):[/bold]")
        recent_table = Table(show_lines=False)
        recent_table.add_column("Time", style="dim")
        recent_table.add_column("Method", style="cyan")
        recent_table.add_column("Host", style="yellow")
        recent_table.add_column("Status", style="green")
        recent_table.add_column("User-Agent", style="magenta", no_wrap=False)

        for detail in sorted(
            user_agent_details, key=lambda x: x["time"], reverse=True
        )[:10]:
            recent_table.add_row(
                detail["time"].strftime("%Y-%m-%d %H:%M:%S"),
                detail["method"],
                detail["host"],
                str(detail["status"]) if detail["status"] else "N/A",
                detail["user_agent"][:80] + "..." if len(detail["user_agent"]) > 80 else detail["user_agent"],
            )

        console.print(recent_table)

    except asyncpg.exceptions.InvalidCatalogNameError:
        console.print(
            "[bold red]Error:[/bold red] Database 'litellm' does not exist"
        )
        console.print(
            "[yellow]Tip:[/yellow] Make sure the postgres container is running and initialized"
        )
    except Exception as e:
        console.print(f"[bold red]Error:[/bold red] {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(get_user_agent_stats())
