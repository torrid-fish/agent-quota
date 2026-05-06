from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from curl_cffi import requests

from common import format_eta, get_cached_or_fetch, load_cookies


# ==================== Configuration ====================

CONFIG_PATH = Path("~/.config/waybar-ai-usage/copilot.conf").expanduser()
DEFAULT_QUOTA = 300
GITHUB_API_BASE = "https://api.github.com"
COPILOT_FEATURES_URL = "https://github.com/settings/copilot/features"


def load_copilot_config(config_path: Path | None = None) -> dict:
    """Load Copilot config from file. Returns dict with GITHUB_TOKEN and COPILOT_QUOTA."""
    path = config_path or CONFIG_PATH
    config: dict = {"GITHUB_TOKEN": None, "COPILOT_QUOTA": DEFAULT_QUOTA}

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
            if key == "GITHUB_TOKEN":
                config["GITHUB_TOKEN"] = value
            elif key == "COPILOT_QUOTA":
                try:
                    config["COPILOT_QUOTA"] = int(value)
                except ValueError:
                    pass

    return config


# ==================== Core Logic: Get Usage ====================

class CopilotHTTPError(RuntimeError):
    """Raised for HTTP errors from the GitHub API, carrying the numeric status code."""
    def __init__(self, code: int, body: str) -> None:
        super().__init__(f"HTTP {code}: {body}")
        self.code = code


def _github_get(url: str, token: str) -> dict | list:
    """Make authenticated GET request to GitHub API."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "waybar-ai-usage/copilot",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        raise CopilotHTTPError(e.code, body) from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Network error: {e.reason}") from e


def _get_github_username(token: str) -> str:
    """Fetch and cache the authenticated GitHub username (TTL: 1 hour)."""
    data = get_cached_or_fetch(
        "copilot_user",
        lambda: _github_get(f"{GITHUB_API_BASE}/user", token),
        ttl=3600,
    )
    username = data.get("login") if isinstance(data, dict) else None
    if not username:
        raise RuntimeError("Could not determine GitHub username from /user endpoint")
    return username


def _fetch_copilot_usage_uncached(token: str) -> dict:
    """Fetch Copilot premium request usage from GitHub API (not cached)."""
    username = _get_github_username(token)
    url = f"{GITHUB_API_BASE}/users/{username}/settings/billing/premium_request/usage"
    usage_data = _github_get(url, token)

    # Response may be a list directly or a dict with usageItems
    if isinstance(usage_data, list):
        usage_items = usage_data
    else:
        usage_items = usage_data.get("usageItems", [])

    used = sum(item.get("grossQuantity", 0) for item in usage_items)
    return {"used": round(used, 1), "raw": usage_data}


def _fetch_copilot_usage_from_browser() -> dict:
    """Fetch Copilot usage percentage from the authenticated Copilot settings page.

    This is a fallback for organization-managed Copilot accounts, where the
    user billing API does not expose premium request usage. The page itself
    renders a usage percentage for the currently signed-in account.
    """
    cookies, browser_name = load_cookies("github.com")

    response = requests.get(
        COPILOT_FEATURES_URL,
        cookies=cookies,
        impersonate="chrome",
        timeout=20,
        allow_redirects=True,
    )
    if response.status_code != 200:
        raise RuntimeError(f"{browser_name}: HTTP {response.status_code}")

    html = response.text
    if 'id="copilot-overages-usage"' not in html:
        raise RuntimeError(f"{browser_name}: no copilot usage section found")

    section_match = re.search(
        r'<div id="copilot-overages-usage".*?</li>',
        html,
        re.S,
    )
    if not section_match:
        raise RuntimeError(f"{browser_name}: usage section parse failed")

    pct_match = re.search(r'>\s*(\d+(?:\.\d+)?)%\s*<', section_match.group(0))
    if not pct_match:
        raise RuntimeError(f"{browser_name}: no usage percentage found")

    managed_by = re.search(
        r'Managed by\s*<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>',
        html,
    )
    return {
        "pct": float(pct_match.group(1)),
        "raw": {
            "managed_by_name": managed_by.group(2) if managed_by else None,
            "managed_by_href": managed_by.group(1) if managed_by else None,
        },
        "source": f"{browser_name}:copilot-features",
    }


def _should_fallback_to_browser(error: Exception) -> bool:
    """Only fall back for user billing API responses that are expected for org-managed Copilot."""
    return isinstance(error, CopilotHTTPError) and error.code in (400, 403, 404)


def get_copilot_usage(token: str | None) -> dict:
    """Fetch Copilot usage with file-based caching (TTL: 60 seconds)."""
    def fetch_browser() -> dict:
        return get_cached_or_fetch("copilot_browser", _fetch_copilot_usage_from_browser)

    if not token:
        return fetch_browser()

    try:
        return get_cached_or_fetch("copilot", lambda: _fetch_copilot_usage_uncached(token))
    except Exception as exc:
        if not _should_fallback_to_browser(exc):
            raise
        return fetch_browser()


# ==================== Output: CLI ====================

def _next_month_reset_iso() -> str:
    """Return ISO timestamp for 00:00 UTC on the 1st of next month."""
    now = datetime.now(timezone.utc)
    if now.month == 12:
        reset = datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        reset = datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    return reset.isoformat()


def print_cli(used: float, quota: int) -> None:
    """Print usage to terminal (for debugging)."""
    pct = round(used / quota * 100) if quota > 0 else 0
    reset_str = format_eta(_next_month_reset_iso())
    print(f"GitHub Copilot Premium Requests")
    print("-" * 40)
    print(f"Used : {used} / {quota} ({pct}%)")
    print(f"Reset: {reset_str} (next month, 1st at 00:00 UTC)")


# ==================== CLI Entry Point ====================

def main() -> None:
    parser = argparse.ArgumentParser(description="Print GitHub Copilot premium request usage to terminal.")
    parser.add_argument(
        "--config",
        type=Path,
        default=CONFIG_PATH,
        help=f"Path to copilot config file (default: {CONFIG_PATH})",
    )
    args = parser.parse_args()

    config = load_copilot_config(args.config)
    token = config["GITHUB_TOKEN"]
    quota = config["COPILOT_QUOTA"]

    try:
        usage = get_copilot_usage(token)
        used = usage.get("used", 0)
        pct = usage.get("pct")
        if pct is not None:
            used = round(quota * float(pct) / 100, 1)
    except Exception as e:
        if not token:
            print(f"[!] No GITHUB_TOKEN in {args.config}", file=sys.stderr)
            print("    For personal Copilot, create a fine-grained PAT with 'Plan (read)' permission.", file=sys.stderr)
            print("    For organization-managed Copilot, log into GitHub in any browser and check:", file=sys.stderr)
            print(f"    {COPILOT_FEATURES_URL}", file=sys.stderr)
        print(f"[!] {e}", file=sys.stderr)
        sys.exit(1)

    print_cli(used, quota)


if __name__ == "__main__":
    main()
