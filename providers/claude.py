from __future__ import annotations

import argparse
import sys

from curl_cffi import requests

from common import format_eta, get_cached_or_fetch, load_cookies, parse_window_percent


# ==================== Configuration ====================

CLAUDE_DOMAIN = "claude.ai"
ORGANIZATIONS_URL = f"https://{CLAUDE_DOMAIN}/api/organizations"
ACCOUNT_URL = f"https://{CLAUDE_DOMAIN}/api/account"

BASE_HEADERS = {
    "Referer": "https://claude.ai/chats",
    "Origin": "https://claude.ai",
    "Accept": "application/json, text/plain, */*",
}


# ==================== Core Logic: Get Usage ====================


def _select_claude_org(
    organizations_data: object, org_id: str | None, account_data: dict | None = None
) -> dict:
    organizations = organizations_data if isinstance(organizations_data, list) else []
    if org_id:
        for org in organizations:
            if isinstance(org, dict) and str(org.get("uuid") or "") == org_id:
                return org

    memberships = (account_data or {}).get("memberships") or []
    for membership in memberships:
        if not isinstance(membership, dict):
            continue
        org = membership.get("organization")
        if isinstance(org, dict):
            return org

    for org in organizations:
        if isinstance(org, dict):
            return org
    return {}


def _extract_claude_identity(
    organizations_data: object,
    account_data: dict,
    org_id: str | None,
) -> dict:
    org = _select_claude_org(organizations_data, org_id, account_data)
    return {
        "plan": (
            org.get("raven_type")
            or org.get("plan")
            or org.get("subscription_plan")
            or org.get("billing_type")
        )
        or "",
        "team_name": org.get("name") or "",
        "organization_id": org.get("uuid") or org.get("id") or org_id or "",
        "organization_billing_type": org.get("billing_type") or "",
        "organization_rate_tier": org.get("rate_limit_tier") or "",
        "user_name": account_data.get("full_name") or account_data.get("display_name") or "",
        "account_name": account_data.get("email_address") or "",
        "account_id": account_data.get("uuid") or account_data.get("tagged_id") or "",
    }


def extract_claude_identity(raw: dict) -> dict:
    identity = raw.get("identity")
    merged = dict(identity) if isinstance(identity, dict) else {}
    fresh = _extract_claude_identity(
        raw.get("_organizations") or [],
        raw.get("_account") or {},
        raw.get("_organization_id"),
    )
    for key, value in fresh.items():
        if value not in (None, ""):
            merged[key] = value
    return merged

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

    usage_url = f"https://{CLAUDE_DOMAIN}/api/organizations/{org_id}/usage"

    # Retry once (2 attempts total)
    last_error = None
    for attempt in range(2):
        try:
            orgs_resp = requests.get(
                ORGANIZATIONS_URL,
                cookies=cookies,
                headers=BASE_HEADERS,
                impersonate="chrome",
                timeout=10
            )
            if orgs_resp.status_code == 403:
                raise RuntimeError("403 Forbidden: Try updating browser_cookie3 or refresh the page in browser.")
            orgs_resp.raise_for_status()
            organizations_data = orgs_resp.json()

            account_resp = requests.get(
                ACCOUNT_URL,
                cookies=cookies,
                headers=BASE_HEADERS,
                impersonate="chrome",
                timeout=10
            )
            if account_resp.status_code == 403:
                raise RuntimeError("403 Forbidden: Try updating browser_cookie3 or refresh the page in browser.")
            account_resp.raise_for_status()
            account_data = account_resp.json()

            resp = requests.get(
                usage_url,
                cookies=cookies,
                headers=BASE_HEADERS,
                impersonate="chrome",
                timeout=10
            )

            if resp.status_code == 403:
                raise RuntimeError("403 Forbidden: Try updating browser_cookie3 or refresh the page in browser.")

            resp.raise_for_status()
            usage_data = resp.json()
            if isinstance(usage_data, dict):
                usage_data["_organization_id"] = org_id
                usage_data["_organizations"] = organizations_data
                usage_data["_account"] = account_data
                usage_data["identity"] = _extract_claude_identity(
                    organizations_data, account_data, org_id
                )
            return usage_data

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
    data = get_cached_or_fetch("claude", lambda: _fetch_claude_usage_uncached(browsers))
    if isinstance(data, dict) and not data.get("identity"):
        # Refresh immediately when a pre-identity cache entry is still fresh.
        data = get_cached_or_fetch(
            "claude", lambda: _fetch_claude_usage_uncached(browsers), ttl=0
        )
    return data


# ==================== Output: CLI ====================

def print_cli(usage: dict) -> None:
    fh = parse_window_percent(usage.get("five_hour"))
    sd = parse_window_percent(usage.get("seven_day"))
    sn = parse_window_percent(usage.get("seven_day_sonnet"))
    identity = extract_claude_identity(usage)

    def _fmt_reset(win):
        if win.utilization == 0 and win.resets_at is None:
            return "Not started"
        return format_eta(win.resets_at)

    print(f"Plan              : {identity.get('plan') or 'Unknown'}")
    if identity.get("team_name"):
        print(f"Team              : {identity['team_name']}")
    print(
        f"User              : "
        f"{identity.get('user_name') or identity.get('account_name') or 'Unknown'}"
    )
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
