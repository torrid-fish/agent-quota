"""agent-quota: show AI provider quotas as a TUI table."""

from __future__ import annotations

import argparse
import sys
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from rich.console import Console, ConsoleOptions, Group, RenderResult
from rich.live import Live
from rich.measure import Measurement
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

from common import format_eta, parse_window_direct, parse_window_percent

CONFIG_PATH = Path("~/.config/agent-quota/config.toml").expanduser()


@dataclass(frozen=True)
class ProviderMeta:
    name: str
    desc: str
    mode: str  # usage | payg
    secret_key: str | None = None
    secret_path: Path | None = None
    secret_label: str | None = None


# Static metadata used by setup/picker. Keep ordered — setup walks this dict
# in registration order, which is also the table row order within each section.
PROVIDER_META: dict[str, ProviderMeta] = {
    "claude": ProviderMeta(
        "Claude", "Claude.ai 5h / 7d quota (browser cookies)", "usage"
    ),
    "codex": ProviderMeta(
        "Codex", "ChatGPT Codex 5h / weekly quota (browser cookies)", "usage"
    ),
    "copilot": ProviderMeta(
        "Copilot",
        "GitHub Copilot premium requests (PAT or browser cookies)",
        "usage",
        "GITHUB_TOKEN",
        Path("~/.config/agent-quota/copilot.conf").expanduser(),
        "GitHub PAT",
    ),
    "zai": ProviderMeta(
        "Z.ai",
        "Z.ai 5h tokens / monthly tools (API token)",
        "usage",
        "ZAI_TOKEN",
        Path("~/.config/agent-quota/zai.conf").expanduser(),
        "Z.ai API token",
    ),
    "zen": ProviderMeta("OpenCode Zen", "OpenCode Zen balance (browser cookies)", "payg"),
    "go": ProviderMeta(
        "OpenCode Go", "OpenCode Go 5h / weekly / monthly usage (browser cookies)", "usage"
    ),
    "openrouter": ProviderMeta(
        "OpenRouter",
        "OpenRouter prepaid credits (management key)",
        "payg",
        "OPENROUTER_API_KEY",
        Path("~/.config/agent-quota/openrouter.conf").expanduser(),
        "OpenRouter management key",
    ),
    "deepseek": ProviderMeta(
        "DeepSeek",
        "DeepSeek API balance (API key)",
        "payg",
        "DEEPSEEK_API_KEY",
        Path("~/.config/agent-quota/deepseek.conf").expanduser(),
        "DeepSeek API key",
    ),
}


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
    mode: str
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
    sn = parse_window_percent(raw.get("seven_day_sonnet"))
    return [
        Metric("5h", f"{fh.utilization:.0f}%", fh.utilization, _window_reset(fh)),
        Metric("7d", f"{sd.utilization:.0f}%", sd.utilization, _window_reset(sd)),
        Metric(
            "7d Sonnet", f"{sn.utilization:.0f}%", sn.utilization, _window_reset(sn)
        ),
    ]


def _adapt_codex(raw: dict) -> list[Metric]:
    rate = raw.get("rate_limit") or {}
    p = parse_window_direct(rate.get("primary_window"))
    s = parse_window_direct(rate.get("secondary_window"))
    return [
        Metric("5h", f"{p.utilization:.0f}%", p.utilization, _window_reset(p)),
        Metric("Weekly", f"{s.utilization:.0f}%", s.utilization, _window_reset(s)),
    ]


def _copilot_reset() -> str:
    # Premium quota refills at the start of each calendar month (UTC).
    now = datetime.now(timezone.utc)
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return format_eta(datetime(year, month, 1, tzinfo=timezone.utc).isoformat())


def _adapt_copilot_factory(quota: int):
    def adapt(raw: dict) -> list[Metric]:
        reset = _copilot_reset()
        if "pct" in raw:
            pct = float(raw["pct"])
            return [Metric("Premium", f"{pct:.0f}%", pct, reset)]
        used = float(raw.get("used", 0.0))
        pct = (used / quota * 100) if quota > 0 else None
        value = f"{used:g} / {quota}" if quota > 0 else f"{used:g}"
        return [Metric("Premium", value, pct, reset)]

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


def _adapt_go(raw: dict) -> list[Metric]:
    rows = [
        ("5h", "rollingUsage"),
        ("Weekly", "weeklyUsage"),
        ("Monthly", "monthlyUsage"),
    ]
    windows = raw.get("windows") or {}
    metrics: list[Metric] = []
    for label, key in rows:
        w = windows.get(key)
        if not w:
            continue
        pct = float(w["usage_percent"])
        if pct == 0 or not w["reset_in_sec"]:
            reset = "—"
        else:
            reset = format_eta(time.time() + w["reset_in_sec"])
        metrics.append(Metric(label, f"{pct:.0f}%", pct, reset))
    return metrics


def _adapt_openrouter(raw: dict) -> list[Metric]:
    remaining = float(raw.get("remaining_credits", 0.0))
    total = float(raw.get("total_credits", 0.0))
    used = float(raw.get("total_usage", 0.0))
    return [
        Metric(
            "Credits",
            f"${remaining:.2f} remaining (${used:.2f} / ${total:.2f} used)",
            None,
            "—",
        )
    ]


def _adapt_deepseek(raw: dict) -> list[Metric]:
    balances = raw.get("balances") or []
    metrics: list[Metric] = []
    for item in balances:
        currency = str(item.get("currency") or "?")
        total = str(item.get("total_balance") or "0")
        granted = str(item.get("granted_balance") or "0")
        topped_up = str(item.get("topped_up_balance") or "0")
        metrics.append(
            Metric(
                currency,
                f"{total} total ({granted} granted, {topped_up} topped up)",
                None,
                "—",
            )
        )
    return metrics


# ===== Fetchers (lazy import so a missing optional dep doesn't kill the whole tool) =====


def _fetch_claude(browsers):
    from providers.claude import get_claude_usage

    return get_claude_usage(browsers)


def _fetch_codex(browsers):
    from providers.codex import get_codex_usage

    return get_codex_usage(browsers)


def _fetch_copilot(browsers):
    from providers.copilot import get_copilot_usage, load_copilot_config

    cfg = load_copilot_config()
    return get_copilot_usage(cfg.get("GITHUB_TOKEN"))


def _fetch_zai(browsers):
    from providers.zai import get_zai_quota, load_zai_config

    cfg = load_zai_config()
    token = cfg.get("ZAI_TOKEN")
    if not token:
        raise RuntimeError("ZAI_TOKEN not set in ~/.config/agent-quota/zai.conf")
    return get_zai_quota(token)


def _fetch_zen(browsers):
    from providers.zen import get_zen_balance

    return get_zen_balance(browsers)


def _fetch_go(browsers):
    from providers.go import get_go_usage

    return get_go_usage(browsers)


def _fetch_openrouter(browsers):
    from providers.openrouter import get_openrouter_balance, load_openrouter_config

    cfg = load_openrouter_config()
    token = cfg.get("OPENROUTER_API_KEY")
    if not token:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set in ~/.config/agent-quota/openrouter.conf"
        )
    return get_openrouter_balance(token)


def _fetch_deepseek(browsers):
    from providers.deepseek import get_deepseek_balance, load_deepseek_config

    cfg = load_deepseek_config()
    token = cfg.get("DEEPSEEK_API_KEY")
    if not token:
        raise RuntimeError(
            "DEEPSEEK_API_KEY not set in ~/.config/agent-quota/deepseek.conf"
        )
    return get_deepseek_balance(token)


@dataclass
class _Provider:
    name: str
    mode: str
    fetch: Callable[[list[str] | None], dict]
    adapt: Callable[[dict], list[Metric]]


def _build_providers() -> dict[str, _Provider]:
    # Copilot quota lives in its own config file; load once so the adapter can
    # format used/quota.
    from providers.copilot import load_copilot_config

    copilot_quota = load_copilot_config().get("COPILOT_QUOTA") or 300
    adapters = {
        "claude": _adapt_claude,
        "codex": _adapt_codex,
        "copilot": _adapt_copilot_factory(copilot_quota),
        "zai": _adapt_zai,
        "zen": _adapt_zen,
        "go": _adapt_go,
        "openrouter": _adapt_openrouter,
        "deepseek": _adapt_deepseek,
    }
    fetchers = {
        "claude": _fetch_claude,
        "codex": _fetch_codex,
        "copilot": _fetch_copilot,
        "zai": _fetch_zai,
        "zen": _fetch_zen,
        "go": _fetch_go,
        "openrouter": _fetch_openrouter,
        "deepseek": _fetch_deepseek,
    }
    return {
        key: _Provider(
            name=meta.name,
            mode=meta.mode,
            fetch=fetchers[key],
            adapt=adapters[key],
        )
        for key, meta in PROVIDER_META.items()
    }


# ===== agent-quota config (which providers are enabled) =====


def load_config() -> dict | None:
    """Load ~/.config/agent-quota/config.toml. Returns None if missing or unreadable."""
    if not CONFIG_PATH.exists():
        return None
    try:
        with open(CONFIG_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return None


def save_config(enabled: list[str]) -> None:
    """Persist the enabled-provider list as TOML.

    Hand-written rather than using a TOML writer dep — the schema is one line.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    body = ", ".join(f'"{k}"' for k in enabled)
    CONFIG_PATH.write_text(
        "# agent-quota configuration\n"
        "# Edit this file or run `agent-quota setup` to change.\n"
        f"enabled = [{body}]\n"
    )


def _load_secret_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        found_key, _, value = line.partition("=")
        if found_key.strip() == key:
            value = value.strip()
            return value or None
    return None


def _save_secret_value(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# agent-quota provider credentials\n"
        f"{key}={value}\n"
    )


def _maybe_prompt_for_provider_secret(console: Console, meta: ProviderMeta) -> None:
    if not meta.secret_key or not meta.secret_path or not meta.secret_label:
        return

    existing = _load_secret_value(meta.secret_path, meta.secret_key)
    if existing:
        if not Confirm.ask(
            f"  Update saved {meta.secret_label}?",
            default=False,
        ):
            return
    else:
        if not Confirm.ask(
            f"  Enter {meta.secret_label} now?",
            default=True,
        ):
            return

    value = Prompt.ask(
        f"  {meta.secret_key}",
        password=True,
    ).strip()
    if not value:
        console.print("  [yellow]Skipped empty secret.[/]")
        return

    _save_secret_value(meta.secret_path, meta.secret_key, value)
    console.print(f"  [green]✓ saved[/] [dim]{meta.secret_path}[/]")


def run_setup() -> int:
    """Interactive picker for enabled providers. Writes config.toml and returns 0."""
    console = Console()
    console.print()
    console.rule("[bold]agent-quota setup[/]")
    console.print(
        "Pick which providers to monitor. Re-run "
        "[cyan]agent-quota setup[/] later to change.\n"
    )

    existing = (load_config() or {}).get("enabled") or list(PROVIDER_META)
    enabled: list[str] = []
    for key, meta in PROVIDER_META.items():
        name = meta.name
        desc = meta.desc
        console.print(f"  [bold]{name:<8}[/]  [dim]{desc}[/]")
        if Confirm.ask(f"  Enable {name}?", default=key in existing):
            enabled.append(key)
            _maybe_prompt_for_provider_secret(console, meta)
        console.print()

    if not enabled:
        console.print("[yellow]No providers selected — config not saved.[/]")
        return 1

    save_config(enabled)
    console.print(f"[green]✓ saved[/] [dim]{CONFIG_PATH}[/]")
    return 0


def _resolve_keys(args, all_providers: dict[str, _Provider]) -> list[str] | None:
    """Decide which provider keys to run for this invocation.

    Precedence: --only > config.toml > interactive setup prompt > all providers.
    Returns None if user input is invalid (caller exits 2).
    """
    if args.only:
        keys = [k.strip().lower() for k in args.only.split(",") if k.strip()]
        unknown = [k for k in keys if k not in all_providers]
        if unknown:
            sys.stderr.write(f"Unknown provider(s): {', '.join(unknown)}\n")
            sys.stderr.write(f"Known: {', '.join(all_providers)}\n")
            return None
        return keys

    cfg = load_config()
    if cfg is not None and "enabled" in cfg:
        keys = [k for k in cfg["enabled"] if k in all_providers]
        if keys:
            return keys

    # No --only and no usable config. Offer to set up if interactive; otherwise
    # fall back to running everything so cron/pipe usage still works.
    if sys.stdin.isatty() and sys.stdout.isatty():
        Console().print(f"[yellow]No agent-quota config at[/] [dim]{CONFIG_PATH}[/]")
        if Confirm.ask("Run setup now?", default=True):
            run_setup()
            cfg = load_config()
            if cfg and cfg.get("enabled"):
                return [k for k in cfg["enabled"] if k in all_providers]

    return list(all_providers)


# ===== Error classification =====

_AUTH_HINTS = (
    "403",
    "401",
    "404",
    "unauthorized",
    "forbidden",
    "cookie",
    "token",
    "lastactiveorg",
)


def _classify(exc: Exception) -> str:
    msg = str(exc).lower()
    return "auth_err" if any(h in msg for h in _AUTH_HINTS) else "net_err"


# ===== Fetch orchestration =====


def fetch_one(key: str, prov: _Provider, browsers: list[str] | None) -> ProviderStatus:
    status = ProviderStatus(key=key, name=prov.name, mode=prov.mode)
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


def fetch_all(
    providers: dict[str, _Provider], browsers: list[str] | None
) -> list[ProviderStatus]:
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

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
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

    def __rich_measure__(
        self, console: Console, options: ConsoleOptions
    ) -> Measurement:
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


def _render_table(
    statuses: list[ProviderStatus], *, title: str, label_header: str, value_header: str
) -> Table:
    table = Table(
        title=title,
        title_style="bold",
        show_header=True,
        header_style="bold cyan",
        expand=True,
    )
    # vertical="middle" lets the single-line Provider/Status/Reset cells sit
    # centered next to multi-line Window/Usage cells when a provider has
    # multiple metrics (e.g. Claude's 5h + 7d).
    table.add_column(
        Text("Provider", justify="center"), no_wrap=True, vertical="middle"
    )
    table.add_column(Text("Status", justify="center"), no_wrap=True, vertical="middle")
    table.add_column(
        Text(label_header, justify="center"), no_wrap=True, vertical="middle"
    )
    # Custom Text header so we can center "Usage" without setting
    # justify="center" on the column itself — that would shrink the _UsageBar
    # to its measured minimum and pad around it instead of filling the column.
    table.add_column(
        Text(value_header, justify="center"),
        no_wrap=True,
        ratio=1,
        min_width=14,
        overflow="ellipsis",
        vertical="middle",
    )
    table.add_column(Text("Reset", justify="right"), no_wrap=True, vertical="middle")

    last_idx = len(statuses) - 1

    for s_idx, s in enumerate(statuses):
        end_section = s_idx != last_idx
        state = Text(_STATE_LABEL[s.state], style=_STATE_STYLE[s.state])

        if s.state != "ok":
            table.add_row(
                s.name,
                state,
                "—",
                Text(s.error or "(no detail)", style="dim"),
                "—",
                end_section=end_section,
            )
            continue
        if not s.metrics:
            table.add_row(
                s.name,
                state,
                "—",
                Text("no data", style="dim"),
                "—",
                end_section=end_section,
            )
            continue

        # Pad name/status with leading blank lines so they land on the visual
        # middle row. For odd n this is the exact center; for even n it sits on
        # the lower of the two middle rows (Rich's vertical="middle" rounds
        # down on its own, which leaves the name stuck at the top).
        n = len(s.metrics)
        pad = "\n" * (n // 2)
        windows = Text("\n".join(m.label for m in s.metrics))
        resets = Text("\n".join(m.reset for m in s.metrics))
        usage = Group(*(_usage_cell(m) for m in s.metrics))
        name_cell = Text(pad + s.name)
        state_cell = Text(pad) + state
        table.add_row(
            name_cell, state_cell, windows, usage, resets, end_section=end_section
        )
    return table


def _render_payg_table(statuses: list[ProviderStatus]) -> Table:
    table = Table(
        title="Pay-As-You-Go Quota",
        title_style="bold",
        show_header=True,
        header_style="bold cyan",
        expand=True,
    )
    table.add_column(
        Text("Provider", justify="center"), no_wrap=True, vertical="middle"
    )
    table.add_column(
        Text("Quota", justify="center"),
        ratio=1,
        min_width=14,
        overflow="ellipsis",
        vertical="middle",
    )

    last_idx = len(statuses) - 1
    for s_idx, s in enumerate(statuses):
        end_section = s_idx != last_idx
        if s.state != "ok":
            err_style = _STATE_STYLE[s.state]
            label = _STATE_LABEL[s.state]
            detail = s.error or "(no detail)"
            table.add_row(
                s.name,
                Text(f"{label} — {detail}", style=err_style),
                end_section=end_section,
            )
            continue
        if not s.metrics:
            table.add_row(s.name, Text("no data", style="dim"), end_section=end_section)
            continue
        # Single-metric providers (Zen today) just show the value; multi-metric
        # providers stack "<label>  <value>" lines so the label still
        # disambiguates rows.
        if len(s.metrics) == 1:
            quota_cell = Text(s.metrics[0].value)
        else:
            quota_cell = Text(
                "\n".join(f"{m.label}  {m.value}" for m in s.metrics)
            )
        table.add_row(s.name, quota_cell, end_section=end_section)
    return table


def render_tables(statuses: list[ProviderStatus]):
    usage_statuses = [s for s in statuses if s.mode == "usage"]
    payg_statuses = [s for s in statuses if s.mode == "payg"]
    tables = []
    if usage_statuses:
        tables.append(
            _render_table(
                usage_statuses,
                title="Usage-Based Limits",
                label_header="Window",
                value_header="Usage",
            )
        )
    if payg_statuses:
        tables.append(_render_payg_table(payg_statuses))
    if not tables:
        return _render_table(
            [],
            title="agent-quota",
            label_header="Window",
            value_header="Usage",
        )
    if len(tables) == 1:
        return tables[0]
    return Group(*tables)


# ===== Main =====


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="agent-quota",
        description="Show AI provider quotas in a TUI table.",
    )
    parser.add_argument(
        "--only",
        metavar="LIST",
        help="Comma-separated providers (overrides config). Known: claude,codex,copilot,zai,zen,go,openrouter,deepseek",
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
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("setup", help="Pick which providers to enable (writes config.toml).")

    args = parser.parse_args()

    if args.command == "setup":
        return run_setup()

    all_providers = _build_providers()
    keys = _resolve_keys(args, all_providers)
    if keys is None:
        return 2
    providers = {k: all_providers[k] for k in keys}

    console = Console()
    browsers = args.browser

    if args.watch is None:
        statuses = fetch_all(providers, browsers)
        console.print(render_tables(statuses))
        return 0 if all(s.state == "ok" for s in statuses) else 1

    interval = max(1, args.watch)
    try:
        with Live(console=console, refresh_per_second=4, screen=True) as live:
            while True:
                statuses = fetch_all(providers, browsers)
                live.update(render_tables(statuses))
                time.sleep(interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
