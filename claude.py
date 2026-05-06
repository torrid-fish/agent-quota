from __future__ import annotations

import argparse
import sys

from curl_cffi import requests

from common import format_eta, get_cached_or_fetch, load_cookies, parse_window_percent


# ==================== Configuration ====================

CLAUDE_DOMAIN = "claude.ai"

BASE_HEADERS = {
    "Referer": "https://claude.ai/chats",
    "Origin": "https://claude.ai",
    "Accept": "application/json, text/plain, */*",
}


# ==================== Core Logic: Get Usage ====================

def _fetch_claude_usage_uncached(browsers: list[str] | None = None) -> dict:
    """Internal function to fetch Claude usage data without caching"""
    try:
        cookies, _browser = load_cookies(CLAUDE_DOMAIN, browsers)
    except Exception as e:
        raise RuntimeError(f"Failed to read cookies: {e}")

    org_id = cookies.get("lastActiveOrg")
    if not org_id:
        raise RuntimeError(
            "Missing 'lastActiveOrg' in cookies.\n"
            "Please refresh Claude page in browser or switch Organization."
        )

    url = f"https://{CLAUDE_DOMAIN}/api/organizations/{org_id}/usage"

    # Retry once (2 attempts total)
    last_error = None
    for attempt in range(2):
        try:
            resp = requests.get(
                url,
                cookies=cookies,
                headers=BASE_HEADERS,
                impersonate="chrome",
                timeout=10
            )

            if resp.status_code == 403:
                raise RuntimeError("403 Forbidden: Try updating browser_cookie3 or refresh the page in browser.")

            resp.raise_for_status()
            return resp.json()

        except Exception as e:
            last_error = e
            if attempt == 0:  # First failure, retry
                continue

    # Both attempts failed
    raise RuntimeError(f"Request failed: {last_error}")


def get_claude_usage(browsers: list[str] | None = None) -> dict:
    """
    Fetch Claude usage data using curl_cffi to impersonate Chrome.

    Uses file-based caching to prevent multiple Waybar instances (one per monitor)
    from making concurrent API requests that might be rate-limited.
    """
    return get_cached_or_fetch("claude", lambda: _fetch_claude_usage_uncached(browsers))


# ==================== Output: CLI ====================

def print_cli(usage: dict) -> None:
    fh = parse_window_percent(usage.get("five_hour"))
    sd = parse_window_percent(usage.get("seven_day"))
    sn = parse_window_percent(usage.get("seven_day_sonnet"))

    def _fmt_reset(win):
        if win.utilization == 0 and win.resets_at is None:
            return "Not started"
        return format_eta(win.resets_at)

    print(f"5-hour       : {fh.utilization:.1f}%  (Reset in {_fmt_reset(fh)})")
    print(f"7-day        : {sd.utilization:.1f}%  (Reset in {_fmt_reset(sd)})")
    print(f"7-day Sonnet : {sn.utilization:.1f}%  (Reset in {_fmt_reset(sn)})")


# ==================== CLI Entry Point ====================

def main() -> None:
    parser = argparse.ArgumentParser(description="Print Claude.ai usage to terminal.")
    parser.add_argument(
        "--browser",
        action="append",
        help="Browser cookie source to try (repeatable). Example: --browser chromium",
    )
    args = parser.parse_args()

    try:
        usage = get_claude_usage(args.browser)
    except Exception as e:
        print(f"[!] {e}", file=sys.stderr)
        sys.exit(1)

    print_cli(usage)


if __name__ == "__main__":
    main()
