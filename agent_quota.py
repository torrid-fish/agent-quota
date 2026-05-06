"""agent-quota: show AI provider quotas as a TUI table."""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from rich.console import Console, ConsoleOptions, RenderResult
from rich.live import Live
from rich.measure import Measurement
from rich.table import Table
from rich.text import Text

from common import format_eta, parse_window_direct, parse_window_percent


# ===== Normalized data model =====

@dataclass
class Metric:
    label: str
    value: str
    pct: float | None = None
    reset: str = "—"


@dataclass
class ProviderStatus:
    key: str
    name: str
    state: str = "ok"  # ok | auth_err | net_err
    metrics: list[Metric] = field(default_factory=list)
    error: str = ""


# ===== Adapters: provider raw dict -> Metric list =====

def _window_reset(win) -> str:
    if not win.resets_at:
        return "—"
    if win.utilization == 0:
        return "—"
    return format_eta(win.resets_at)


def _adapt_claude(raw: dict) -> list[Metric]:
    fh = parse_window_percent(raw.get("five_hour"))
    sd = parse_window_percent(raw.get("seven_day"))
    return [
        Metric("5h", f"{fh.utilization:.0f}%", fh.utilization, _window_reset(fh)),
        Metric("7d", f"{sd.utilization:.0f}%", sd.utilization, _window_reset(sd)),
    ]


def _adapt_codex(raw: dict) -> list[Metric]:
    rate = raw.get("rate_limit") or {}
    p = parse_window_direct(rate.get("primary_window"))
    s = parse_window_direct(rate.get("secondary_window"))
    return [
        Metric("5h", f"{p.utilization:.0f}%", p.utilization, _window_reset(p)),
        Metric("Weekly", f"{s.utilization:.0f}%", s.utilization, _window_reset(s)),
    ]


def _adapt_copilot_factory(quota: int):
    def adapt(raw: dict) -> list[Metric]:
        if "pct" in raw:
            pct = float(raw["pct"])
            return [Metric("Premium", f"{pct:.0f}%", pct, "monthly")]
        used = float(raw.get("used", 0.0))
        pct = (used / quota * 100) if quota > 0 else None
        value = f"{used:g} / {quota}" if quota > 0 else f"{used:g}"
        return [Metric("Premium", value, pct, "monthly")]
    return adapt


def _fmt_tokens(v: float | int) -> str:
    v = int(v)
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}K"
    return str(v)


def _ms_reset(ms: int | None) -> str:
    if not ms:
        return "—"
    return format_eta(ms // 1000)


def _adapt_zai(raw: dict) -> list[Metric]:
    metrics: list[Metric] = []
    tl = raw.get("token_limit")
    if tl:
        pct = float(tl.get("percentage", 0))
        used = tl.get("usedTokens") or tl.get("used") or 0
        total = tl.get("totalTokens") or tl.get("total") or 0
        if total:
            value = f"{_fmt_tokens(used)} / {_fmt_tokens(total)}"
        else:
            value = f"{pct:.0f}%"
        metrics.append(Metric("Tokens", value, pct, _ms_reset(tl.get("nextResetTime"))))

    ml = raw.get("time_limit")
    if ml:
        pct = float(ml.get("percentage", 0))
        remaining = ml.get("remaining")
        value = f"{pct:.0f}%" if remaining is None else f"{pct:.0f}% ({remaining} left)"
        metrics.append(Metric("Tools", value, pct, _ms_reset(ml.get("nextResetTime"))))

    return metrics


def _adapt_zen(raw: dict) -> list[Metric]:
    bal = float(raw.get("balance", 0.0))
    return [Metric("Balance", f"${bal:.2f}", None, "—")]


# ===== Fetchers (lazy import so a missing optional dep doesn't kill the whole tool) =====

def _fetch_claude(browsers):
    from claude import get_claude_usage
    return get_claude_usage(browsers)


def _fetch_codex(browsers):
    from codex import get_codex_usage
    return get_codex_usage(browsers)


def _fetch_copilot(browsers):
    from copilot import get_copilot_usage, load_copilot_config
    cfg = load_copilot_config()
    return get_copilot_usage(cfg.get("GITHUB_TOKEN"))


def _fetch_zai(browsers):
    from zai import get_zai_quota, load_zai_config
    cfg = load_zai_config()
    token = cfg.get("ZAI_TOKEN")
    if not token:
        raise RuntimeError("ZAI_TOKEN not set in ~/.config/agent-quota/zai.conf")
    return get_zai_quota(token)


def _fetch_zen(browsers):
    from zen import get_zen_balance
    return get_zen_balance(browsers)


@dataclass
class _Provider:
    name: str
    fetch: Callable[[list[str] | None], dict]
    adapt: Callable[[dict], list[Metric]]


def _build_providers() -> dict[str, _Provider]:
    # Copilot quota lives in the config file; load once so the adapter can format used/quota.
    from copilot import load_copilot_config
    copilot_quota = load_copilot_config().get("COPILOT_QUOTA") or 300
    return {
        "claude":  _Provider("Claude",  _fetch_claude,  _adapt_claude),
        "codex":   _Provider("Codex",   _fetch_codex,   _adapt_codex),
        "copilot": _Provider("Copilot", _fetch_copilot, _adapt_copilot_factory(copilot_quota)),
        "zai":     _Provider("Z.ai",    _fetch_zai,     _adapt_zai),
        "zen":     _Provider("Zen",     _fetch_zen,     _adapt_zen),
    }


# ===== Error classification =====

_AUTH_HINTS = ("403", "401", "404", "unauthorized", "forbidden", "cookie", "token", "lastactiveorg")


def _classify(exc: Exception) -> str:
    msg = str(exc).lower()
    return "auth_err" if any(h in msg for h in _AUTH_HINTS) else "net_err"


# ===== Fetch orchestration =====

def fetch_one(key: str, prov: _Provider, browsers: list[str] | None) -> ProviderStatus:
    status = ProviderStatus(key=key, name=prov.name)
    try:
        raw = prov.fetch(browsers)
    except Exception as exc:
        status.state = _classify(exc)
        status.error = str(exc).splitlines()[0][:120]
        return status
    try:
        status.metrics = prov.adapt(raw)
    except Exception as exc:
        status.state = "net_err"
        status.error = f"adapter: {exc}"
    return status


def fetch_all(providers: dict[str, _Provider], browsers: list[str] | None) -> list[ProviderStatus]:
    results: dict[str, ProviderStatus] = {}
    with ThreadPoolExecutor(max_workers=max(1, len(providers))) as pool:
        futs = {pool.submit(fetch_one, k, p, browsers): k for k, p in providers.items()}
        for fut in as_completed(futs):
            k = futs[fut]
            results[k] = fut.result()
    return [results[k] for k in providers]  # preserve registration order


# ===== Rendering =====

_STATE_STYLE = {"ok": "green", "auth_err": "red", "net_err": "yellow"}
_STATE_LABEL = {"ok": "OK", "auth_err": "Auth Err", "net_err": "Net Err"}


class _UsageBar:
    """Full-width progress bar with the metric value overlaid in the centre."""

    def __init__(self, pct: float, value: str) -> None:
        self.pct = max(0.0, min(100.0, pct))
        self.value = value

    def _color(self) -> str:
        if self.pct < 70:
            return "green"
        if self.pct < 90:
            return "yellow"
        return "red"

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = options.max_width
        if width <= 0:
            yield Text("")
            return
        filled = int(self.pct / 100 * width + 0.5)
        text = self.value[:width]
        text_start = (width - len(text)) // 2
        text_end = text_start + len(text)
        color = self._color()

        text_color_on_filled = "black" if color == "yellow" else "white"
        result = Text()
        for i in range(width):
            char = text[i - text_start] if text_start <= i < text_end else " "
            if i < filled:
                result.append(char, style=f"bold {text_color_on_filled} on {color}")
            else:
                result.append(char, style=f"bold {color} on grey23")
        yield result

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        # Floor needs to be high enough that the bar still reads as a bar even
        # when options.max_width is small or 0 (which happens during the first
        # measure pass in Live mode). Returning Measurement(0, 0) here would
        # collapse the column and make the bar disappear.
        minimum = max(len(self.value) + 4, 12)
        maximum = max(minimum, options.max_width)
        return Measurement(minimum, maximum)


def _usage_cell(metric: Metric):
    if metric.pct is None:
        return Text(metric.value)
    return _UsageBar(metric.pct, metric.value)


def render_table(statuses: list[ProviderStatus]) -> Table:
    table = Table(
        title="agent-quota",
        title_style="bold",
        show_header=True,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column("Provider", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Window", no_wrap=True)
    table.add_column("Usage", no_wrap=True, ratio=1, min_width=14, overflow="ellipsis")
    table.add_column("Reset", justify="right", no_wrap=True)

    last_idx = len(statuses) - 1

    for s_idx, s in enumerate(statuses):
        is_last_provider = s_idx == last_idx
        state = Text(_STATE_LABEL[s.state], style=_STATE_STYLE[s.state])

        if s.state != "ok":
            table.add_row(
                s.name, state, "—", Text(s.error or "(no detail)", style="dim"), "—",
                end_section=not is_last_provider,
            )
            continue
        if not s.metrics:
            table.add_row(
                s.name, state, "—", Text("no data", style="dim"), "—",
                end_section=not is_last_provider,
            )
            continue

        last_metric = len(s.metrics) - 1
        for i, m in enumerate(s.metrics):
            end_section = (i == last_metric) and not is_last_provider
            table.add_row(
                s.name if i == 0 else "",
                state if i == 0 else "",
                m.label,
                _usage_cell(m),
                m.reset,
                end_section=end_section,
            )
    return table


# ===== Main =====

def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-quota",
        description="Show AI provider quotas in a TUI table.",
    )
    parser.add_argument(
        "--only",
        metavar="LIST",
        help="Comma-separated providers (default: all). Known: claude,codex,copilot,zai,zen",
    )
    parser.add_argument(
        "--watch",
        type=int,
        nargs="?",
        const=15,
        metavar="SECS",
        help="Refresh every SECS seconds (default 15 if SECS omitted). Ctrl+C to exit.",
    )
    parser.add_argument(
        "--browser",
        action="append",
        metavar="NAME",
        help="Browser preference for cookie-auth providers; repeatable.",
    )
    args = parser.parse_args()

    all_providers = _build_providers()

    if args.only:
        keys = [k.strip().lower() for k in args.only.split(",") if k.strip()]
        unknown = [k for k in keys if k not in all_providers]
        if unknown:
            sys.stderr.write(f"Unknown provider(s): {', '.join(unknown)}\n")
            sys.stderr.write(f"Known: {', '.join(all_providers)}\n")
            return 2
        providers = {k: all_providers[k] for k in keys}
    else:
        providers = all_providers

    console = Console()
    browsers = args.browser

    if args.watch is None:
        statuses = fetch_all(providers, browsers)
        console.print(render_table(statuses))
        return 0 if all(s.state == "ok" for s in statuses) else 1

    interval = max(1, args.watch)
    try:
        with Live(console=console, refresh_per_second=4, screen=False) as live:
            while True:
                statuses = fetch_all(providers, browsers)
                live.update(render_table(statuses))
                time.sleep(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
