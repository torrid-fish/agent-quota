# agent-quota

One command, one terminal table, your current quota across **Claude**, **OpenAI Codex**, **GitHub Copilot**, **OpenCode Zen**, and **Z.ai**.

```
                                  agent-quota
┏━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┓
┃ Provider ┃ Status   ┃ Window  ┃ Usage                              ┃   Reset ┃
┡━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━┩
│ Claude   │ OK       │ 5h      │ ████████▓▓▓▓▓  23%  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓ │   1h17m │
│          │          │ 7d      │ ████████▓▓▓▓▓  23%  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓ │   5d02h │
├──────────┼──────────┼─────────┼────────────────────────────────────┼─────────┤
│ Codex    │ OK       │ 5h      │ ▓▓▓▓▓▓▓▓▓▓▓▓▓▓  0%  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓ │       — │
│          │          │ Weekly  │ ▓▓▓▓▓▓▓▓▓▓▓▓▓  3%  ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ │   5d23h │
├──────────┼──────────┼─────────┼────────────────────────────────────┼─────────┤
│ Copilot  │ OK       │ Premium │ ▓▓▓▓▓▓▓▓▓▓▓  0 / 300  ▓▓▓▓▓▓▓▓▓▓▓▓ │ monthly │
├──────────┼──────────┼─────────┼────────────────────────────────────┼─────────┤
│ Z.ai     │ OK       │ Tokens  │ ███████▓▓▓▓ 1.2K / 5.0M  ▓▓▓▓▓▓▓▓▓ │   2h08m │
├──────────┼──────────┼─────────┼────────────────────────────────────┼─────────┤
│ Zen      │ OK       │ Balance │ $19.43                             │       — │
└──────────┴──────────┴─────────┴────────────────────────────────────┴─────────┘
```

The Usage column is a coloured progress bar that fills its allocated width with the metric value overlaid in the centre. Bar colour shifts at 70% (yellow) and 90% (red); rows without a percentage (e.g. Zen's balance) render as plain text. The table itself expands to fill the terminal width, so wider terminals get longer bars.

Forked from [waybar-ai-usage](https://github.com/NihilDigit/waybar-ai-usage) — same providers, no Waybar / Wayland / Linux dependency. Just a terminal.

## Install

```bash
# from a clone
uv sync
uv run agent-quota

# or install as a uv tool
uv tool install .
agent-quota
```

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

## Usage

```bash
agent-quota                       # one-shot table (uses ~/.config/agent-quota/config.toml)
agent-quota setup                 # interactive picker for which providers to enable
agent-quota --watch               # auto-refresh every 15s
agent-quota --watch 30            # custom interval
agent-quota --only claude,codex   # one-off subset (overrides config)
agent-quota --browser firefox     # cookie source for cookie-auth providers
```

Press `Ctrl+C` to exit watch mode. Exit code is `0` if every selected provider is OK, `1` otherwise.

## First run

The first time you run `agent-quota` without a config file, it prompts you to pick which providers to monitor and saves your selection to `~/.config/agent-quota/config.toml`:

```toml
# agent-quota configuration
# Edit this file or run `agent-quota setup` to change.
enabled = ["claude", "codex", "copilot"]
```

Re-run `agent-quota setup` any time to change the selection. Pass `--only` to override the config for a single invocation. If stdin/stdout aren't a TTY (e.g. running from cron or piped), agent-quota skips the prompt and falls back to all providers so non-interactive use still works.

## Auth per provider

| Provider | Auth | What you need |
|---|---|---|
| Claude | Browser cookies | Be logged into [claude.ai](https://claude.ai) in any supported browser |
| Codex | Browser cookies | Be logged into [chatgpt.com](https://chatgpt.com) |
| Zen | Browser cookies | Be logged into [opencode.ai](https://opencode.ai/zen) |
| Copilot | GitHub PAT *or* browser cookies | Token in `~/.config/agent-quota/copilot.conf`, **or** be logged into github.com (org-managed Copilot) |
| Z.ai | API token (JWT) | Token in `~/.config/agent-quota/zai.conf` |

Supported cookie sources: `chrome`, `chromium`, `brave`, `edge`, `firefox`, `helium`. The first one that has a valid session wins. Override order with `--browser <name>` (repeatable).

### Copilot config

```ini
# ~/.config/agent-quota/copilot.conf
GITHUB_TOKEN=github_pat_...   # fine-grained PAT with "Plan (read)" permission
COPILOT_QUOTA=300             # your monthly included quota; default 300
```

Org-managed Copilot accounts can omit `GITHUB_TOKEN` — the tool falls back to scraping `github.com/settings/copilot/features` using your browser cookies.

### Z.ai config

```ini
# ~/.config/agent-quota/zai.conf
ZAI_TOKEN=eyJ...              # JWT from DevTools (Network tab → api.z.ai → Authorization header)
```

The Z.ai JWT can't be auto-refreshed; if it expires, copy a fresh one from DevTools.

## Caching

Results are cached in `~/.cache/agent-quota/<provider>.json` (TTL: 60s, Z.ai 120s). Concurrent runs (e.g. `--watch` + a one-shot) coordinate via `.updating` marker files.

## Per-provider CLI

Each provider also has a standalone debug CLI:

```bash
uv run python -m providers.claude
uv run python -m providers.codex
uv run python -m providers.copilot
uv run python -m providers.zai
uv run python -m providers.zen
uv run python -m providers.go
```

These are useful for diagnosing a specific provider in isolation.

## License

MIT — see [LICENSE](LICENSE).
