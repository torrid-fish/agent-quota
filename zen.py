"""OpenCode Zen balance fetcher.

Fetches current Zen balance from the OpenCode dashboard using browser cookies.
"""

from __future__ import annotations

import argparse
import re
import sys

from curl_cffi import requests

from common import get_cached_or_fetch, load_cookies


# ==================== Configuration ====================

ZEN_DOMAIN = "opencode.ai"
ZEN_URL = "https://opencode.ai/auth"

BASE_HEADERS = {
    "Referer": "https://opencode.ai/auth",
    "Origin": "https://opencode.ai",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CACHE_TTL = 120  # Cache for 120 seconds


# ==================== Core Logic: Get Balance ====================


def _parse_balance_from_html(html_content: str) -> float | None:
    """Parse balance from HTML content using various patterns"""

    # Pattern 1: JS state object — balance:<number> (integer or decimal)
    # e.g. balance:0  or  balance:19.43
    balance_match = re.search(
        r"balance:([0-9]+(?:\.[0-9]+)?)",
        html_content,
    )
    if balance_match:
        return float(balance_match.group(1))

    # Pattern 2: data-slot="balance" structure with HTML comments (legacy)
    balance_match = re.search(
        r'data-slot="balance"[^>]*>.*?Current balance.*?<b>\$\s*<!--\$-->([0-9]+\.[0-9]{2})<!--/-->',
        html_content,
        re.DOTALL,
    )
    if balance_match:
        return float(balance_match.group(1))

    # Pattern 3: Simple "Current balance $XX.XX" pattern (legacy)
    balance_match = re.search(
        r"Current balance\s*\$\s*([0-9]+\.[0-9]{2})",
        html_content,
    )
    if balance_match:
        return float(balance_match.group(1))

    return None


def _fetch_zen_balance_uncached(browsers: list[str] | None = None) -> dict:
    """Internal function to fetch Zen balance without caching"""
    try:
        cookies, _browser = load_cookies(ZEN_DOMAIN, browsers)
    except Exception as e:
        raise RuntimeError(f"Failed to read cookies: {e}")

    # Try zen URL first - it redirects to the specific workspace
    last_error = None
    for attempt in range(2):
        try:
            resp = requests.get(
                ZEN_URL,
                cookies=cookies,
                headers=BASE_HEADERS,
                impersonate="chrome",
                timeout=10,
                allow_redirects=True,  # Follow redirects to specific workspace
            )

            if resp.status_code == 403:
                raise RuntimeError(
                    "403 Forbidden: Try updating browser_cookie3 or refresh the page in browser."
                )

            resp.raise_for_status()

            # Parse the HTML to find the balance
            html_content = resp.text

            balance = _parse_balance_from_html(html_content)

            if balance is not None:
                return {"balance": balance, "currency": "USD"}
            else:
                raise RuntimeError(
                    "Could not find balance. Please ensure you're logged into opencode.ai/zen in your browser."
                )

        except Exception as e:
            last_error = e
            if attempt == 0:  # First failure, retry
                continue

    # All attempts failed
    raise RuntimeError(f"Request failed: {last_error}")


def get_zen_balance(browsers: list[str] | None = None) -> dict:
    """
    Fetch Zen balance using curl_cffi to impersonate Chrome.
    Uses file-based caching to prevent multiple Waybar instances from making
    concurrent API requests.
    """
    return get_cached_or_fetch(
        "zen-balance", lambda: _fetch_zen_balance_uncached(browsers), ttl=CACHE_TTL
    )


# ==================== Output: CLI / Waybar ====================


def print_cli(balance_data: dict) -> None:
    """Print balance to terminal (for debugging)."""
    print(f"Zen Balance: ${balance_data['balance']:.2f} {balance_data['currency']}")


# ==================== CLI Entry Point ====================


def main() -> None:
    parser = argparse.ArgumentParser(description="Print OpenCode Zen balance to terminal.")
    parser.add_argument(
        "--browser",
        action="append",
        help="Browser cookie source to try (repeatable). Example: --browser chromium",
    )
    args = parser.parse_args()

    try:
        balance_data = get_zen_balance(args.browser)
    except Exception as e:
        print(f"[!] {e}", file=sys.stderr)
        sys.exit(1)

    print_cli(balance_data)


if __name__ == "__main__":
    main()
