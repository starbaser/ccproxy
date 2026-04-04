from rich.console import Console
from rich.theme import Theme

srcery_colors = {
    "black": "#1c1b19",
    "red": "#ef2f27",
    "green": "#519f50",
    "yellow": "#fbb829",
    "blue": "#2c78bf",
    "magenta": "#e02c6d",
    "cyan": "#0aaeb3",
    "white": "#baa67f",
    "orange": "#ff5f00",
    "bright_black": "#918175",
    "bright_red": "#f75341",
    "bright_green": "#98bc37",
    "bright_yellow": "#fed06e",
    "bright_blue": "#68a8e4",
    "bright_magenta": "#ff5c8f",
    "bright_cyan": "#2be4d0",
    "bright_white": "#fce8c3",
    "bright_orange": "#ff8700",
    "xgray1": "#262626",
    "xgray2": "#303030",
    "xgray3": "#3a3a3a",
    "xgray4": "#444444",
}

theme = Theme(srcery_colors)
console = Console(theme=theme, style="on black", width=120, force_terminal=True)

DIAGRAM = """

    [cyan]###[/] [red]ccproxy run --inspect[/] [cyan]- the namespace jail[/]

    [white]Host[/]
    [bright_black]┌───────────────────────────────────────────────────────────────────────────────────┐[/]
    [bright_black]│[/]         [yellow]▲[/] [white]regular (outbound to providers)[/]                                         [bright_black]│[/]
    [bright_black]│[/]         [yellow]│[/]                                                                         [bright_black]│[/]
    [bright_black]│[/]  [blue]┌──────[/][yellow]┴[/][blue]─────┐[/]    [white]reverse[/]     [green]┌─────────┐[/]                                        [bright_black]│[/]
    [bright_black]│[/]  [blue]│[/] [bright_white]mitmweb[/]    [blue]│[/][yellow]◀──────────────▶[/][green]│[/] [bright_white]LiteLLM[/] [green]│[/]                                        [bright_black]│[/]
    [bright_black]│[/]  [blue]│[/]            [blue]│[/]    [orange]@:4000[/]      [green]└────[/][yellow]┬[/][green]────┘[/]                                        [bright_black]│[/]
    [bright_black]│[/]  [blue]│[/]            [blue]│[/][yellow]◀────────────────────┘[/] [white]HTTPS_PROXY[/] [orange]@:8081[/]                          [bright_black]│[/]
    [bright_black]│[/]  [blue]│[/] [white]WG srv[/]     [blue]│[/]                                                                   [bright_black]│[/]
    [bright_black]│[/]  [blue]│[/] [orange]@:51820[/]    [blue]│[/]                                                                   [bright_black]│[/]
    [bright_black]│[/]  [blue]└────────────┘[/]                                                                   [bright_black]│[/]
    [bright_black]│[/]       [yellow]▲[/]                                                                           [bright_black]│[/]
    [bright_black]│[/]       [yellow]│[/] [white]WireGuard UDP (via host network)[/]                                          [bright_black]│[/]
    [bright_black]│[/]       [yellow]▼[/]                                                                           [bright_black]│[/]
    [bright_black]│[/]  [magenta]┌─────────────────────────────────────────────────────────────┐[/]                  [bright_black]│[/]
    [bright_black]│[/]  [magenta]│[/] [bright_white]slirp4netns (bridges namespace ◀▶ host)[/]                     [magenta]│[/]                  [bright_black]│[/]
    [bright_black]│[/]  [magenta]│[/] [white]host gateway:[/] [cyan]10.0.2.2[/]                                      [magenta]│[/]                  [bright_black]│[/]
    [bright_black]│[/]  [magenta]└─────────────────────────────────────────────────────────────┘[/]                  [bright_black]│[/]
    [bright_black]│[/]                                                                                   [bright_black]│[/]
    [bright_black]│[/]  [bright_black]┌────────────────[/] [bright_white]Network Namespace (user+net, no root)[/] [bright_black]───────────────────────┐[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/]                                                                              [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/] [yellow]tap0[/] [white]─▶[/] [cyan]10.0.2.100/24[/]  [white](slirp4netns --configure)[/]                             [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/] [yellow]wg0[/]  [white]─▶[/] [cyan]10.0.0.1/32[/]    [white](WireGuard client)[/]                                    [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/] [white]Endpoint =[/] [cyan]10.0.2.2:51820[/] [white](─▶ host mitmweb via slirp)[/]                        [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/] [white]default route via[/] [yellow]wg0[/]                                                        [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/]                                                                              [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/]  [bright_blue]┌───────────────────┐[/]                                                       [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/]  [bright_blue]│[/] [bright_white]<confined process>[/][bright_blue]│[/]      [white]all traffic ─▶[/] [yellow]wg0[/]                               [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/]  [bright_blue]│[/] [white](e.g. claude CLI)[/] [bright_blue]│[/]      [white]─▶ mitmweb captures[/]                              [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]│[/]  [bright_blue]└───────────────────┘[/]                                                       [bright_black]│[/] [bright_black]│[/]
    [bright_black]│[/]  [bright_black]└──────────────────────────────────────────────────────────────────────────────┘[/] [bright_black]│[/]
    [bright_black]└───────────────────────────────────────────────────────────────────────────────────┘[/]
"""

console.print(DIAGRAM)
