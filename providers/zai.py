from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

from common import format_eta, get_cached_or_fetch


# ==================== Configuration ====================

CONFIG_PATH = Path("~/.config/agent-quota/zai.conf").expanduser()
API_BASE = "https://api.z.ai"
QUOTA_URL = f"{API_BASE}/api/monitor/usage/quota/limit"


def load_zai_config(config_path: Path | None = None) -> dict:
    path = config_path or CONFIG_PATH
    config: dict = {"ZAI_TOKEN": None}

    if not path.exists():
        return config

    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            if key == "ZAI_TOKEN":
                config["ZAI_TOKEN"] = value

    return config


# ==================== Core Logic: Get Quota ====================


def _api_get(url: str, token: str) -> dict:
    # Z.ai's monitor API expects the raw token in the Authorization header,
    # without the "Bearer " prefix. GLM Coding Plan tokens are rejected
    # with 401 otherwise.
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": token,
            "Accept": "application/json",
            "Accept-Language": "en-US,en",
            "Content-Type": "application/json",
            "User-Agent": "agent-quota/zai",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def _fetch_zai_quota_uncached(token: str) -> dict:
    data = _api_get(QUOTA_URL, token)

    if not data.get("success"):
        msg = data.get("msg", "Unknown error")
        raise RuntimeError(f"API error: {msg}")

    limits = data.get("data", {}).get("limits", [])

    token_limit = None
    time_limit = None

    for item in limits:
        if item.get("type") == "TOKENS_LIMIT":
            token_limit = item
        elif item.get("type") == "TIME_LIMIT":
            time_limit = item

    return {
        "token_limit": token_limit,
        "time_limit": time_limit,
        "level": data.get("data", {}).get("level"),
    }


def get_zai_quota(token: str) -> dict:
    return get_cached_or_fetch(
        "zai",
        lambda: _fetch_zai_quota_uncached(token),
        ttl=120,
    )


# ==================== Helpers ====================


def _format_tokens(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _format_ms_reset(ms: int | None) -> str:
    if not ms:
        return "??"
    return format_eta(ms // 1000)


# ==================== Output: CLI ====================


def print_cli(quota: dict) -> None:
    tl = quota.get("token_limit")
    ml = quota.get("time_limit")

    print(f"Z.ai Usage (level: {quota.get('level', '?')})")
    print("-" * 50)

    if tl:
        pct = max(0, 100 - float(tl.get("percentage", 0)))
        reset = _format_ms_reset(tl.get("nextResetTime"))
        print(f"5h Tokens remaining : {pct:.0f}%  (Reset in {reset})")

    if ml:
        pct = max(0, 100 - float(ml.get("percentage", 0)))
        remaining = ml.get("remaining", 0)
        reset = _format_ms_reset(ml.get("nextResetTime"))
        print(f"Monthly Tools remaining: {pct:.0f}% ({remaining} remaining)")
        for d in ml.get("usageDetails", []):
            code = d.get("modelCode", "?")
            usage = d.get("usage", 0)
            print(f"  - {code}: {usage}")


# ==================== CLI Entry Point ====================


def main() -> None:
    parser = argparse.ArgumentParser(description="Print Z.ai usage to terminal.")
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help=f"Path to Z.ai config file (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()

    config = load_zai_config(args.config)
    token = config["ZAI_TOKEN"]

    if not token:
        print(f"[!] No ZAI_TOKEN in {args.config}", file=sys.stderr)
        print(f"    Web session JWT:", file=sys.stderr)
        print(f"      1. Log into https://z.ai", file=sys.stderr)
        print(f"      2. Open DevTools (F12) > Network tab", file=sys.stderr)
        print(f"      3. Copy the Authorization header value from any api.z.ai", file=sys.stderr)
        print(f"         request (strip the leading 'Bearer ')", file=sys.stderr)
        print(f"    GLM Coding Plan API key: paste it directly.", file=sys.stderr)
        print(f"    Then save ZAI_TOKEN=<token> in {args.config}", file=sys.stderr)
        sys.exit(1)

    try:
        quota = get_zai_quota(token)
    except Exception as e:
        print(f"[!] {e}", file=sys.stderr)
        sys.exit(1)

    print_cli(quota)


if __name__ == "__main__":
    main()
