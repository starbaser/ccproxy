#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "anthropic",
#     "httpx",
#     "plotille",
#     "rich",
# ]
# ///
"""Measure the RPM rate limit behind ccproxy's anthropic provider.

Client pattern follows ccproxy's docs/examples/anthropic_sdk.py: the sentinel
key `sk-ant-oat-ccproxy-anthropic` routes through the proxy on localhost:4000,
which substitutes real credentials and replays the Claude Code envelope.

The controller is a fixed-interval AIMD feedback loop: every tick (default 1s)
it raises the target rate multiplicatively while the last tick was clean, and
on a 429 it halves the rate and pauses the pacer for retry-after. A live
plotille chart shows target RPM, trailing sends/min, trailing ok/min, and 429
events; in a non-TTY the chart prints once at the end.

Usage:
    uv run measure_rpm.py --probe     # one request; print rate-limit headers
    uv run measure_rpm.py             # live AIMD ramp
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import statistics
import time
from collections import deque
from dataclasses import dataclass, field

import anthropic
import httpx
import plotille
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

console = Console()

SENTINEL_KEY = "sk-ant-oat-ccproxy-anthropic"
DEFAULT_BASE_URL = "http://127.0.0.1:4000"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
PROMPT = "hi"
RL_PREFIX = "anthropic-ratelimit-"


@dataclass
class RateLimitInfo:
    rate: float
    trailing: int
    retry_after: float
    exhausted: list[str]
    message: str


@dataclass
class State:
    rate: float
    t0: float
    pause_until: float = 0.0
    stop: bool = False
    stop_reason: str = ""
    sent_times: deque[float] = field(default_factory=deque)
    ok_times: deque[float] = field(default_factory=deque)
    total_sent: int = 0
    ok: int = 0
    rl_hits: int = 0
    other_errors: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    last_headers: dict[str, str] = field(default_factory=dict)
    first_429: RateLimitInfo | None = None
    last_429: RateLimitInfo | None = None
    new_429: RateLimitInfo | None = None
    last_error: str = ""
    last_log: float = 0.0
    h_t: list[float] = field(default_factory=list)
    h_target: list[float] = field(default_factory=list)
    h_sent: list[float] = field(default_factory=list)
    h_ok: list[float] = field(default_factory=list)
    e429_t: list[float] = field(default_factory=list)
    e429_v: list[float] = field(default_factory=list)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--probe", action="store_true", help="send one request, print rate-limit headers, exit")
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--start-rpm", type=float, default=20.0)
    p.add_argument("--tick", type=float, default=1.0, help="controller feedback interval seconds")
    p.add_argument("--growth", type=float, default=1.05, help="rate multiplier per clean tick")
    p.add_argument("--decrease", type=float, default=0.5, help="rate multiplier on a 429 tick")
    p.add_argument("--cooldown", type=float, default=2.0, help="minimum pacer pause seconds after a 429")
    p.add_argument("--min-rpm", type=float, default=5.0)
    p.add_argument("--max-rpm", type=float, default=6000.0)
    p.add_argument("--max-seconds", type=float, default=900.0)
    p.add_argument("--max-requests", type=int, default=8000)
    p.add_argument("--timeout", type=float, default=30.0, help="per-request timeout seconds")
    p.add_argument("--chart-width", type=int, default=72)
    p.add_argument("--chart-height", type=int, default=16)
    return p.parse_args()


def make_client(args: argparse.Namespace) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=SENTINEL_KEY,
        base_url=args.base_url,
        max_retries=0,  # SDK retries would silently absorb the 429 signal
        timeout=args.timeout,
        http_client=anthropic.DefaultAsyncHttpxClient(
            limits=httpx.Limits(max_connections=256, max_keepalive_connections=64),
        ),
    )


def trailing(times: deque[float], now: float, horizon: float = 60.0) -> int:
    while times and now - times[0] > horizon:
        times.popleft()
    return len(times)


def header_limit(st: State) -> int | None:
    raw = st.last_headers.get(f"{RL_PREFIX}requests-limit")
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def rate_limit_info(e: anthropic.RateLimitError, st: State) -> RateLimitInfo:
    headers = {k.lower(): v for k, v in e.response.headers.items()}
    exhausted = [
        k.removeprefix(RL_PREFIX).removesuffix("-remaining")
        for k, v in headers.items()
        if k.startswith(RL_PREFIX) and k.endswith("-remaining") and v == "0"
    ]
    try:
        retry_after = float(headers.get("retry-after", 0) or 0)
    except ValueError:
        retry_after = 0.0
    return RateLimitInfo(
        rate=st.rate,
        trailing=trailing(st.sent_times, time.monotonic()),
        retry_after=retry_after,
        exhausted=exhausted,
        message=str(e)[:200],
    )


async def send_one(client: anthropic.AsyncAnthropic, st: State, args: argparse.Namespace) -> None:
    try:
        raw = await client.messages.with_raw_response.create(
            model=args.model,
            max_tokens=1,
            messages=[{"role": "user", "content": PROMPT}],
        )
        msg = raw.parse()
        st.ok += 1
        st.ok_times.append(time.monotonic())
        st.input_tokens += msg.usage.input_tokens
        st.output_tokens += msg.usage.output_tokens
        st.last_headers = {k.lower(): v for k, v in raw.headers.items() if k.lower().startswith(RL_PREFIX)}
    except anthropic.RateLimitError as e:
        st.rl_hits += 1
        info = rate_limit_info(e, st)
        if st.first_429 is None:
            st.first_429 = info
        st.last_429 = info
        if st.new_429 is None:
            st.new_429 = info
    except anthropic.APIStatusError as e:
        st.other_errors += 1
        st.last_error = f"{e.status_code}: {str(e)[:120]}"
    except anthropic.APIConnectionError as e:
        st.other_errors += 1
        st.last_error = type(e).__name__


async def pacer(
    client: anthropic.AsyncAnthropic, st: State, args: argparse.Namespace, tasks: set[asyncio.Task[None]]
) -> None:
    while not st.stop:
        now = time.monotonic()
        if now < st.pause_until:
            await asyncio.sleep(min(st.pause_until - now, 0.25))
            continue
        st.sent_times.append(now)
        st.total_sent += 1
        task = asyncio.create_task(send_one(client, st, args))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        if st.total_sent >= args.max_requests:
            st.stop = True
            st.stop_reason = "request budget exhausted"
            break
        fired = now
        while not st.stop:
            remaining = fired + 60.0 / st.rate - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 0.25))


def render_chart(st: State, args: argparse.Namespace) -> str:
    fig = plotille.Figure()
    fig.width = args.chart_width
    fig.height = args.chart_height
    fig.color_mode = "names"
    t_max = max(60.0, st.h_t[-1] if st.h_t else 60.0)
    y_max = max([10.0, *st.h_target, *st.h_sent, *st.h_ok, *st.e429_v]) * 1.15
    fig.set_x_limits(min_=0.0, max_=t_max)
    fig.set_y_limits(min_=0.0, max_=y_max)
    if st.h_t:
        fig.plot(st.h_t, st.h_target, lc="yellow", label="target rpm")
        fig.plot(st.h_t, st.h_sent, lc="cyan", label="sent/min")
        fig.plot(st.h_t, st.h_ok, lc="green", label="ok/min")
    if st.e429_t:
        fig.scatter(st.e429_t, st.e429_v, lc="red", label="429")
    return fig.show(legend=True)


def render_status(st: State, now: float) -> Table:
    t = Table.grid(padding=(0, 2))
    t.add_column(style="bold cyan")
    t.add_column()
    paused = now < st.pause_until
    t.add_row("target", f"{st.rate:.0f} rpm" + (" [red](paused)[/red]" if paused else ""))
    t.add_row("trailing", f"{trailing(st.sent_times, now)} sent/min · {trailing(st.ok_times, now)} ok/min")
    t.add_row("totals", f"{st.total_sent} sent · {st.ok} ok · {st.rl_hits} 429 · {st.other_errors} err")
    if st.last_429 is not None:
        t.add_row(
            "last 429",
            f"at {st.last_429.trailing}/min (exhausted: {', '.join(st.last_429.exhausted) or 'n/a'}; "
            f"retry-after {st.last_429.retry_after:g}s)",
        )
    util = st.last_headers.get(f"{RL_PREFIX}unified-5h-utilization")
    if util:
        t.add_row("5h quota", f"{util} utilized")
    if st.last_error:
        t.add_row("last error", st.last_error)
    return t


def build_view(st: State, args: argparse.Namespace, now: float) -> Group:
    return Group(Text.from_ansi(render_chart(st, args)), render_status(st, now))


async def controller(st: State, args: argparse.Namespace, live: Live | None) -> None:
    next_tick = time.monotonic()
    while not st.stop:
        next_tick += args.tick
        await asyncio.sleep(max(0.0, next_tick - time.monotonic()))
        now = time.monotonic()
        t = now - st.t0

        info = st.new_429
        st.new_429 = None
        if info is not None:
            st.e429_t.append(t)
            st.e429_v.append(float(info.trailing))
            st.rate = max(args.min_rpm, st.rate * args.decrease)
            st.pause_until = now + max(info.retry_after, args.cooldown)
        elif now >= st.pause_until:
            st.rate = min(args.max_rpm, st.rate * args.growth)

        st.h_t.append(t)
        st.h_target.append(st.rate)
        st.h_sent.append(float(trailing(st.sent_times, now)))
        st.h_ok.append(float(trailing(st.ok_times, now)))

        if live is not None:
            live.update(build_view(st, args, now))
        elif now - st.last_log >= 10.0:
            st.last_log = now
            console.log(
                f"t={t:.0f}s target={st.rate:.0f} sent/min={st.h_sent[-1]:.0f} "
                f"ok/min={st.h_ok[-1]:.0f} 429s={st.rl_hits} err={st.other_errors}"
            )

        if t >= args.max_seconds:
            st.stop = True
            st.stop_reason = "time budget exhausted"


def print_summary(st: State, elapsed: float) -> None:
    t = Table(title="RPM ramp summary", show_header=False)
    t.add_column(style="bold cyan")
    t.add_column()
    t.add_row("outcome", st.stop_reason or "unknown")
    t.add_row("peak sent/min", f"{max(st.h_sent):.0f}" if st.h_sent else "n/a")
    t.add_row("peak ok/min", f"{max(st.h_ok):.0f}" if st.h_ok else "n/a")
    if st.e429_v:
        t.add_row(
            "429 ceiling estimates (sends/min)",
            f"median {statistics.median(st.e429_v):.0f} · peak {max(st.e429_v):.0f} · n={len(st.e429_v)}",
        )
    else:
        t.add_row("429 ceiling", "no 429 observed")
    if st.first_429 is not None:
        t.add_row("first 429 exhausted limits", ", ".join(st.first_429.exhausted) or "n/a")
        t.add_row("first 429 retry-after", f"{st.first_429.retry_after:g}s")
        t.add_row("first 429 message", st.first_429.message)
    lim = header_limit(st)
    t.add_row("advertised requests-limit header", str(lim) if lim else "not seen")
    t.add_row("requests sent / ok / 429 / other", f"{st.total_sent} / {st.ok} / {st.rl_hits} / {st.other_errors}")
    t.add_row("tokens in / out", f"{st.input_tokens} / {st.output_tokens}")
    t.add_row("wall time", f"{elapsed:.0f}s")
    console.print(t)


async def run_ramp(args: argparse.Namespace) -> None:
    client = make_client(args)
    st = State(rate=max(args.min_rpm, min(args.start_rpm, args.max_rpm)), t0=time.monotonic())
    tasks: set[asyncio.Task[None]] = set()
    interactive = console.is_terminal
    live = Live(console=console, refresh_per_second=4) if interactive else None
    if not interactive:
        console.log(f"ramp start: {args.model} via {args.base_url} at {st.rate:.0f} rpm (tick {args.tick:g}s)")
    pacer_task: asyncio.Task[None] | None = None
    try:
        with live if live is not None else contextlib.nullcontext():
            pacer_task = asyncio.create_task(pacer(client, st, args, tasks))
            await controller(st, args, live)
            st.stop = True
            await pacer_task
    except asyncio.CancelledError:
        st.stop = True
        st.stop_reason = st.stop_reason or "interrupted"
        if pacer_task is not None:
            pacer_task.cancel()
        for task in tasks:
            task.cancel()
    finally:
        st.stop = True
        if tasks:
            try:
                await asyncio.wait(tasks, timeout=args.timeout + 5)
            except (asyncio.CancelledError, Exception):
                pass
        await client.close()
        if not interactive:
            console.print(Text.from_ansi(render_chart(st, args)))
        print_summary(st, time.monotonic() - st.t0)


async def probe(args: argparse.Namespace) -> None:
    client = make_client(args)
    try:
        t0 = time.monotonic()
        raw = await client.messages.with_raw_response.create(
            model=args.model,
            max_tokens=1,
            messages=[{"role": "user", "content": PROMPT}],
        )
        latency = time.monotonic() - t0
        msg = raw.parse()
        text = msg.content[0].text if msg.content and msg.content[0].type == "text" else ""
        console.print(
            f"[green]ok[/green] {msg.model} in {latency:.2f}s "
            f"({msg.usage.input_tokens} in / {msg.usage.output_tokens} out): {text!r}"
        )
        t = Table(title="response headers of interest")
        t.add_column("header", style="cyan")
        t.add_column("value")
        headers = {k.lower(): v for k, v in raw.headers.items()}
        for k in sorted(headers):
            if k.startswith(RL_PREFIX) or k in ("retry-after", "request-id"):
                t.add_row(k, headers[k])
        console.print(t)
    finally:
        await client.close()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(probe(args) if args.probe else run_ramp(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
