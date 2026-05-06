"""OpenCode Go usage fetcher.

Scrapes the inline JS state on the workspace's /go page to surface the
rolling, weekly, and monthly usage windows. The workspace ID is
auto-discovered by following the /auth redirect, so no per-user config
is needed.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

from curl_cffi import requests

from common import format_eta, get_cached_or_fetch, load_cookies


GO_DOMAIN = "opencode.ai"
AUTH_URL = "https://opencode.ai/auth"

BASE_HEADERS = {
    "Referer": "https://opencode.ai/",
    "Origin": "https://opencode.ai",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CACHE_TTL = 120

_WORKSPACE_RE = re.compile(r"/workspace/(wrk_[A-Za-z0-9]+)")
_WINDOW_KEYS = ("rollingUsage", "weeklyUsage", "monthlyUsage")


def _resolve_workspace(cookies: dict) -> str:
    """Hit /auth, follow the redirect, and pull the workspace id from the URL."""
    resp = requests.get(
        AUTH_URL,
        cookies=cookies,
        headers=BASE_HEADERS,
        impersonate="chrome",
        timeout=10,
        allow_redirects=True,
    )
    if resp.status_code == 403:
        raise RuntimeError(
            "403 Forbidden on /auth: cookies expired? Refresh opencode.ai in your browser."
        )
    resp.raise_for_status()
    m = _WORKSPACE_RE.search(str(resp.url))
    if not m:
        raise RuntimeError(f"Could not locate workspace id in redirect URL: {resp.url}")
    return m.group(1)


def _parse_window(html: str, name: str) -> dict | None:
    """Pull a single window object out of the inline JS state.

    Tolerant of field order and the optional `$R[N]=` Next.js bookkeeping
    prefix that wraps the literal in some builds. The strict colon-to-brace
    matcher matters: keys like `monthlyUsage` also appear as plain integers
    elsewhere in the state, and a lazy matcher would latch onto the wrong
    `{...}` block.
    """
    obj = re.search(
        rf"{name}\s*:\s*(?:\$R\[\d+\]\s*=\s*)?\{{([^}}]*)\}}",
        html,
    )
    if not obj:
        return None
    body = obj.group(1)
    status = re.search(r'status:"([^"]+)"', body)
    reset = re.search(r"resetInSec:(\d+)", body)
    pct = re.search(r"usagePercent:(\d+)", body)
    if not (status and reset and pct):
        return None
    return {
        "status": status.group(1),
        "reset_in_sec": int(reset.group(1)),
        "usage_percent": int(pct.group(1)),
    }


def _fetch_go_usage_uncached(browsers: list[str] | None = None) -> dict:
    try:
        cookies, _browser = load_cookies(GO_DOMAIN, browsers)
    except Exception as e:
        raise RuntimeError(f"Failed to read cookies: {e}")

    last_error = None
    for attempt in range(2):
        try:
            ws_id = _resolve_workspace(cookies)
            resp = requests.get(
                f"https://{GO_DOMAIN}/workspace/{ws_id}/go",
                cookies=cookies,
                headers=BASE_HEADERS,
                impersonate="chrome",
                timeout=10,
                allow_redirects=True,
            )
            if resp.status_code == 403:
                raise RuntimeError(
                    "403 Forbidden on /go: cookies expired? Refresh opencode.ai in your browser."
                )
            resp.raise_for_status()

            windows = {k: _parse_window(resp.text, k) for k in _WINDOW_KEYS}
            windows = {k: v for k, v in windows.items() if v}
            if not windows:
                raise RuntimeError(
                    "Could not parse usage windows on /go. "
                    "Is OpenCode Go enabled for this workspace?"
                )
            return {"workspace": ws_id, "windows": windows}
        except Exception as e:
            last_error = e
            if attempt == 0:
                continue

    raise RuntimeError(f"Request failed: {last_error}")


def get_go_usage(browsers: list[str] | None = None) -> dict:
    return get_cached_or_fetch(
        "go", lambda: _fetch_go_usage_uncached(browsers), ttl=CACHE_TTL
    )


def print_cli(usage: dict) -> None:
    rows = [
        ("5h", "rollingUsage"),
        ("Weekly", "weeklyUsage"),
        ("Monthly", "monthlyUsage"),
    ]
    windows = usage.get("windows") or {}
    for label, key in rows:
        w = windows.get(key)
        if not w:
            continue
        reset = format_eta(time.time() + w["reset_in_sec"]) if w["reset_in_sec"] else "—"
        status = "" if w["status"] == "ok" else f" [{w['status']}]"
        print(f"{label:<8} : {w['usage_percent']:>3}%{status}  (Reset in {reset})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Print OpenCode Go usage to terminal.")
    parser.add_argument(
        "--browser",
        action="append",
        help="Browser cookie source to try (repeatable). Example: --browser chromium",
    )
    args = parser.parse_args()

    try:
        usage = get_go_usage(args.browser)
    except Exception as e:
        print(f"[!] {e}", file=sys.stderr)
        sys.exit(1)

    print_cli(usage)


if __name__ == "__main__":
    main()
