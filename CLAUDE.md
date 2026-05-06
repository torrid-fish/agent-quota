# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A terminal app (`agent-quota`) that prints current quota across **Claude, OpenAI Codex, GitHub Copilot, OpenCode Zen, OpenCode Go, and Z.ai**. Output is now split into two features: a **usage-based limits** table for slice/rate-limit style windows, and a **pay-as-you-go quota** table for balances / credits. Forked from waybar-ai-usage; the Waybar layer is gone. One command, render both sections, exit. Optional `--watch` enters a Rich `Live` refresh loop.

## Mental model

- `agent_quota.py` is the orchestrator: imports each provider's `get_*_usage()` / `get_*_quota()` directly, runs them in parallel via `ThreadPoolExecutor`, normalizes results into `ProviderStatus(metrics: list[Metric], mode: str)` plus optional subscription metadata (`plan`, `user`), and renders one or two Rich tables depending on which provider categories are selected.
- Providers are classified in `PROVIDER_META` as either `usage` or `payg`. That classification drives setup copy, row grouping, and which table a provider appears in.
- The usage table currently has `Provider`, `Plan`, `User`, `Window`, `Usage`, and `Reset` columns. `Plan`/`User` are provider metadata fields, not derived from the bar itself. `Codex` currently has the best support here because its session payload exposes `plan_type`, the signed-in user name, and account structure. Other providers may still render `Unknown` / `—` until a provider-specific identity extractor exists.
- The Usage / Quota value cell is **not** a Rich primitive when `Metric.pct` is present — it's a custom renderable `_UsageBar` that implements `__rich_console__` + `__rich_measure__`. At render time Rich passes the column's allocated width via `options.max_width`, and `_UsageBar` paints a coloured bar (white-on-colour for filled, colour-on-grey23 for empty) with the metric value text centred across both portions. This is why `Metric` carries both `pct` (drives bar fill) and `value` (drives the overlay text) — the adapters in `agent_quota.py` shape the value text per provider so it's meaningful when overlaid (e.g. `"30%"`, `"0 / 300"`, `"1.2K / 5.0M"`). Metrics without `pct` render as plain text, which is how pay-as-you-go balances such as Zen currently display.
- Six provider scripts live under `providers/` (`claude.py`, `codex.py`, `copilot.py`, `zen.py`, `go.py`, `zai.py`). Each one is data-fetch + a tiny `print_cli()` debug `main()`. They depend only on `common.py` (which sits at the repo root, not inside the package, since it's shared with `agent_quota.py`).
- `common.py` is the contract layer: `load_cookies` (multi-browser fallback), `get_cached_or_fetch` (file cache + cross-process `.updating` marker), `format_eta`, `parse_window_*`.

A provider script never imports another provider. The orchestrator (`agent_quota.py`) imports each provider as `providers.<name>` but each fetcher is wrapped in lazy import so an optional missing dep doesn't kill the whole tool.

## Code shape (post-fork)

| File | Lines | Role |
|------|-------|------|
| `agent_quota.py` | ~500 | Orchestrator: parallel fetch, adapters, provider categorisation (`usage` vs `payg`), split Rich rendering (`_UsageBar` overlay), `--watch` loop, setup picker, config.toml I/O |
| `common.py` | ~250 | `load_cookies`, `get_cached_or_fetch`, `format_eta`, `parse_window_*` |
| `providers/claude.py`, `providers/codex.py` | ~115 each | Cookie-auth providers (claude.ai, chatgpt.com) |
| `providers/copilot.py` | ~240 | PAT-auth via `urllib`; Chrome-cookie HTML-scrape fallback for org-managed accounts |
| `providers/zai.py` | ~170 | API-token-auth via `urllib`; JWT must be copied from DevTools |
| `providers/zen.py` | ~180 | Cookie-auth provider (opencode.ai); HTML-scraped balance off `/billing` |
| `providers/go.py` | ~150 | Cookie-auth provider (opencode.ai); HTML-scraped 5h/weekly/monthly windows |

## The provider contract

A provider script must:

1. Expose a `get_<name>_usage()` (or `_quota()` / `_balance()`) function returning a `dict`. Adapters in `agent_quota.py` consume that dict. If the provider exposes account/session metadata, include a normalized `identity` block when practical so `Plan` / `User` can be rendered without heuristic guessing.
2. Wrap the network call in `get_cached_or_fetch("<name>", fetch_fn, ttl=...)` from `common.py`.
3. Read its config (if any) from `~/.config/agent-quota/<name>.conf` via a `load_<name>_config()` function.
4. Provide a `print_cli()` and a thin `main()` for standalone debugging — *not* called from the TUI; the TUI only calls the data-fetch functions.

Reuse from `common.py` rather than rolling your own. `format_eta` accepts ISO 8601 string, Unix timestamp seconds, or `None`.

## Three auth strategies

| Strategy | Used by | Picked when |
|---|---|---|
| **Browser cookie auto-detect** | `providers/claude.py`, `providers/codex.py`, `providers/zen.py`, `providers/go.py` | Cookies persist on disk and the API accepts them. Multi-browser fallback via `DEFAULT_BROWSERS` order in `common.py`. |
| **Static API token in config file** | `providers/copilot.py` (PAT), `providers/zai.py` (JWT) | When auth is short-lived/JS-generated, OR when the user must explicitly grant a scoped token. |
| **HTML scrape with cookies** | `providers/zen.py`, `providers/go.py`, `providers/copilot.py` org-managed fallback | When the API doesn't expose the data but a logged-in HTML page does. The opencode.ai providers also auto-discover the workspace id by following the `/auth` redirect. |

`curl_cffi` with `impersonate="chrome"` is required for any cookie-authenticated call to providers behind Cloudflare (claude.ai, chatgpt.com, opencode.ai, github.com settings page). Stdlib `urllib` is fine for plain API calls (Copilot billing, Z.ai).

## Adding a provider

A new `providers/<name>.py` requires touch-ups in **two places** (the package directory itself is already in `pyproject.toml`):

1. `agent_quota.py`: add a `_fetch_<name>` lazy-import function (`from providers.<name> import …`), an adapter that turns raw dict → `list[Metric]`, an entry in `PROVIDER_META` with the correct `mode` (`usage` or `payg`), and corresponding entries in `_build_providers()`'s `fetchers` and `adapters` dicts.
2. The new `providers/<name>.py` itself, conforming to the provider contract above.

That's it — no UI orchestrator, no CSS, no JSON5 config writer.

Choose the `mode` based on what the numbers mean:

- `usage`: the provider reports utilization against resettable slices/windows such as 5h, weekly, monthly, token buckets, or included request quotas.
- `payg`: the provider reports a monetary / credit / prepaid balance where the main value is remaining quota rather than consumed percentage.

## Cross-cutting concerns

**Top-level config** (`~/.config/agent-quota/config.toml`)
- One key: `enabled = ["claude", "codex", ...]`. Loaded by `agent_quota.load_config`.
- Written by `agent-quota setup` (interactive picker via `rich.prompt.Confirm`).
- Resolution order in `_resolve_keys`: `--only` flag > config.toml > interactive setup prompt (TTY only) > all providers.
- `--view {usage,payg,both}` post-filters the resolved provider set by `mode` before fetch. Defaults to `both`. Composes with `--only` (AND semantics). If the filter leaves zero providers, the CLI exits 2 with a hint to re-run `setup` or use `--only`.
- Per-provider `~/.config/agent-quota/<name>.conf` files are unrelated — they hold tokens, not the enabled list.

**Caching** (`common.get_cached_or_fetch`)
- File-based: `~/.cache/agent-quota/<name>.json` with TTL (default 60s, Z.ai 120s).
- `.updating` marker files coordinate concurrent fetches (e.g. `--watch` + an interactive one-shot).

**Error UX**
- `agent_quota.fetch_one` classifies exceptions into `auth_err` (HTTP 401/403/404, "cookie", "token", "lastActiveOrg") or `net_err` (everything else). The classification is a substring match on the error message — keep that hint set in sync with what providers actually raise.
- Failed providers render a red/yellow status row with the first line of the error message inside their respective section; OK providers continue rendering normally.

**Retry**
- Cookie-auth providers (`providers/claude.py`, `providers/codex.py`, `providers/zen.py`, `providers/go.py`) retry once on failure (2 attempts, 10s timeout each).
- Token-auth providers (`providers/copilot.py`, `providers/zai.py`) do not retry. Single 10s timeout.
- Total wall-time per provider should stay below `--watch` interval to avoid overlap.

## Fragility map

| Trigger | Affected | Symptom | Fix pattern |
|---|---|---|---|
| Cloudflare anti-bot tightens | claude/codex/zen/go | All requests 403 | Bump `curl_cffi` |
| Provider renames an API field | any | Weird number or `Net Err` | Update field path in provider's adapter in `agent_quota.py` |
| GitHub redesigns settings page | copilot org-fallback | Regex misses, "no copilot usage section" | Update HTML regex in `providers/copilot.py` |
| Wrong browser picked first | cookie-auth | `Missing 'lastActiveOrg'` etc. | User passes `--browser <name>` |
| Firefox uses XDG `~/.config/mozilla/firefox` | cookie-auth on newer distros | Firefox cookies not found | `_firefox_xdg_fallback` in `common.py` |
| Z.ai JWT expires | `providers/zai.py` | `Auth Err` after working for a while | User re-grabs token from DevTools |
| opencode.ai moves the balance off `/billing` or renames `data-slot="balance-value"` | `providers/zen.py` | Garbage balance or "Could not find balance" | Tighten regex in `_parse_balance_from_html`. Do **not** fall back to a loose `balance:<num>` JS-state pattern — the inline state contains an unrelated unix-timestamp-shaped field that will silently produce nonsense numbers. |
| opencode.ai changes the inline JS state for `/go` | `providers/go.py` | Missing rows or "Could not parse usage windows" | The regex requires `name:\s*(?:\$R\[N\]\s*=\s*)?\{…\}` — keep the strict colon-to-brace anchor since `monthlyUsage` also appears as a plain integer elsewhere on the page. |
| `_UsageBar.__rich_measure__` returns `Measurement(0, 0)` | `--watch` mode (Live's first measure pass can pass `options.max_width=0`) | Bars silently disappear, Usage column collapses to 0 cells | Floor `minimum` at ~12 cells regardless of `options.max_width`; `Usage` column also has `min_width=14` as belt-and-braces. Don't undo either guard. |

## Development

```bash
uv run agent-quota                  # default one-shot (reads config.toml)
uv run agent-quota setup            # re-run the interactive picker
uv run agent-quota --watch 5        # auto-refresh
uv run agent-quota --only claude    # one-off subset, ignores config
uv run agent-quota --view usage     # render only the usage-based limits table
uv run agent-quota --view payg      # render only the pay-as-you-go quota table
uv run python -m providers.claude   # debug a single provider in isolation
```

Python 3.11+ (uses `X | Y` union syntax and `tomllib`). Zero global installs — always use `uv run`. No tests, no linter configured; correctness is verified by hand-running each provider against a real account, and rendering changes are eyeballed in a real terminal (Rich falls back to plain text when piped, so `cat`/Bash output won't show colours).

## Distribution

Plain `uv build` → wheel; `uv tool install .` for local install. No PyPI, no AUR yet for this fork.
