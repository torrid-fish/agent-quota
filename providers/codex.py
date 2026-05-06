from __future__ import annotations

import argparse
import sys

from curl_cffi import requests

from common import format_eta, get_cached_or_fetch, load_cookies, parse_window_direct


# ================= Configuration =================

BASE_HEADERS = {
    "Referer": "https://chatgpt.com/",
    "Origin": "https://chatgpt.com",
    "Accept": "*/*"
}

SESSION_URL = "https://chatgpt.com/api/auth/session"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

# ================= Network Logic =================

def _fetch_codex_usage_uncached(browsers: list[str] | None = None) -> dict:
    """Internal function to fetch Codex usage data without caching"""
    try:
        cookies_dict, _browser = load_cookies("chatgpt.com", browsers)
    except Exception as e:
        raise RuntimeError(f"Failed to read browser cookies: {e}")

    # Retry once (2 attempts total)
    last_error = None
    for attempt in range(2):
        try:
            # Get Access Token
            session_resp = requests.get(
                SESSION_URL,
                cookies=cookies_dict,
                headers=BASE_HEADERS,
                impersonate="chrome",
                timeout=10
            )

            if session_resp.status_code == 403:
                raise RuntimeError("403 Forbidden: Cloudflare blocked, check IP or update browser_cookie3")

            session_resp.raise_for_status()
            session_data = session_resp.json()

            access_token = session_data.get("accessToken")
            if not access_token:
                raise RuntimeError("accessToken not found in session response.")

            # Get Usage Data
            usage_headers = BASE_HEADERS.copy()
            usage_headers["Authorization"] = f"Bearer {access_token}"

            usage_resp = requests.get(
                CODEX_USAGE_URL,
                cookies=cookies_dict,
                headers=usage_headers,
                impersonate="chrome",
                timeout=10
            )

            usage_resp.raise_for_status()
            return usage_resp.json()

        except Exception as e:
            last_error = e
            if attempt == 0:  # First failure, retry
                continue

    # Both attempts failed
    raise RuntimeError(f"Request failed: {last_error}")


def get_codex_usage(browsers: list[str] | None = None) -> dict:
    """
    Fetch ChatGPT Codex usage data.

    Uses file-based caching to prevent multiple Waybar instances (one per monitor)
    from making concurrent API requests that might be rate-limited.
    """
    return get_cached_or_fetch("codex", lambda: _fetch_codex_usage_uncached(browsers))


# ================= Output: CLI =================


def print_cli(usage: dict) -> None:
    rate = usage.get("rate_limit") or {}
    p = parse_window_direct(rate.get("primary_window"))
    s = parse_window_direct(rate.get("secondary_window"))

    print(f"Primary   (Short): {p.utilization:>5.1f}% | Reset in {format_eta(p.resets_at)}")
    print(f"Secondary (Long) : {s.utilization:>5.1f}% | Reset in {format_eta(s.resets_at)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print ChatGPT Codex usage to terminal.")
    parser.add_argument(
        "--browser",
        action="append",
        help="Browser cookie source to try (repeatable). Example: --browser chromium",
    )
    args = parser.parse_args()

    try:
        usage = get_codex_usage(args.browser)
    except Exception as e:
        print(f"[!] {e}", file=sys.stderr)
        sys.exit(1)

    print_cli(usage)


if __name__ == "__main__":
    main()
