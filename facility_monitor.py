"""
Wing Partner Portal - Facility Availability Monitor
====================================================
Logs in via Google SSO, scrapes facility availability,
tracks status changes, and posts Slack alerts when
unavailable facilities become available.

Requirements:
    pip install playwright python-dotenv requests
    playwright install chromium

Usage:
    python facility_monitor.py                  # run once
    python facility_monitor.py --loop           # run every hour indefinitely
"""

import os
import json
import time
import argparse
import logging
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv()

PORTAL_URL = (
    "https://partner.wing.com/delivery/organizations/"
    "yQwtUnBngGZU/facilities?csesidx=195873912"
)

GOOGLE_EMAIL    = os.getenv("GOOGLE_EMAIL")       # your Google account email
GOOGLE_PASSWORD = os.getenv("GOOGLE_PASSWORD")    # your Google account password
SLACK_WEBHOOK   = os.getenv("SLACK_WEBHOOK_URL")  # incoming webhook URL

STATE_FILE  = Path("facility_state.json")         # persists last-known statuses
LOG_FILE    = Path("facility_monitor.log")
POLL_HOURS  = 1                                    # how often to re-check

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ── State helpers ─────────────────────────────────────────────────────────────

def load_state() -> dict:
    """Load previously recorded facility statuses from disk."""
    if STATE_FILE.exists():
        with STATE_FILE.open() as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with STATE_FILE.open("w") as f:
        json.dump(state, f, indent=2)

# ── Slack ─────────────────────────────────────────────────────────────────────

def post_slack(message: str) -> None:
    """Send a message to Slack via incoming webhook."""
    if not SLACK_WEBHOOK:
        log.warning("SLACK_WEBHOOK_URL not set – skipping Slack notification.")
        print(f"\n[SLACK] {message}\n")
        return

    payload = {"text": message}
    try:
        resp = requests.post(SLACK_WEBHOOK, json=payload, timeout=10)
        resp.raise_for_status()
        log.info("Slack notification sent.")
    except requests.RequestException as exc:
        log.error("Failed to send Slack message: %s", exc)


def build_slack_message(newly_available: list[dict], still_unavailable: list[dict]) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"*🔔 Wing Facility Status Update* — {ts}"]

    if newly_available:
        lines.append("\n✅ *Now Available* (was Unavailable):")
        for f in newly_available:
            lines.append(f"  • {f['name']}  `{f['id']}`")

    if still_unavailable:
        lines.append("\n🔴 *Still Unavailable:*")
        for f in still_unavailable:
            lines.append(f"  • {f['name']}  `{f['id']}`")

    if not newly_available and not still_unavailable:
        lines.append("\n✅ All previously unavailable facilities are now available!")

    return "\n".join(lines)

# ── Browser / scraping ────────────────────────────────────────────────────────

def scrape_facilities(page) -> list[dict]:
    """
    Return a list of dicts: [{name, id, status}, ...]
    Handles pagination automatically.
    """
    facilities = []
    page_num = 0

    while True:
        page_num += 1
        log.info("Scraping page %d …", page_num)

        # Wait for the table rows to appear
        page.wait_for_selector("table tbody tr", timeout=20_000)

        rows = page.query_selector_all("table tbody tr")
        if not rows:
            break

        for row in rows:
            cells = row.query_selector_all("td")
            if len(cells) < 3:
                continue

            name   = cells[0].inner_text().strip()
            status = cells[1].inner_text().strip()
            fid    = cells[2].inner_text().strip()

            facilities.append({"name": name, "id": fid, "status": status})

        # Try to go to the next page
        next_btn = page.query_selector("button[aria-label='Next page']:not([disabled])")
        if next_btn:
            next_btn.click()
            page.wait_for_load_state("networkidle", timeout=15_000)
        else:
            break

    log.info("Scraped %d facilities total.", len(facilities))
    return facilities


def google_sso_login(page) -> None:
    """
    Navigate to the portal; handle the Google SSO redirect automatically.
    Fills in email + password. If your org uses a third-party IdP on top of
    Google (e.g. Okta), you may need to extend this section.
    """
    log.info("Navigating to portal …")
    page.goto(PORTAL_URL, wait_until="networkidle")

    # If already logged in, we're done
    if "facilities" in page.url and "accounts.google" not in page.url:
        log.info("Already authenticated.")
        return

    log.info("Redirected to Google login …")

    # ── Email step ──
    page.wait_for_selector("input[type='email']", timeout=15_000)
    page.fill("input[type='email']", GOOGLE_EMAIL)
    page.click("#identifierNext, button:has-text('Next')")

    # ── Password step ──
    page.wait_for_selector("input[type='password']", timeout=15_000)
    page.fill("input[type='password']", GOOGLE_PASSWORD)
    page.click("#passwordNext, button:has-text('Next')")

    # ── Wait for redirect back to portal ──
    try:
        page.wait_for_url("**/facilities**", timeout=30_000)
        log.info("Login successful.")
    except PlaywrightTimeoutError:
        # Might need 2FA or a consent screen – save a screenshot for debugging
        page.screenshot(path="login_debug.png")
        raise RuntimeError(
            "Login did not complete automatically. "
            "Check login_debug.png – you may need to handle 2FA manually "
            "or use a saved browser session (see HEADLESS note in README)."
        )


# ── Main logic ────────────────────────────────────────────────────────────────

def run_check() -> None:
    previous_state = load_state()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,                   # set False to watch/debug
            args=["--no-sandbox"],
        )
        context = browser.new_context(
            # Re-use saved cookies/session if available (avoids re-login)
            storage_state="browser_session.json" if Path("browser_session.json").exists() else None,
        )
        page = context.new_page()

        try:
            google_sso_login(page)

            # Save session so the next run skips login
            context.storage_state(path="browser_session.json")

            facilities = scrape_facilities(page)
        finally:
            browser.close()

    if not facilities:
        log.warning("No facilities found – check selectors or login.")
        return

    # ── Compare with previous state ──────────────────────────────────────────
    current_unavailable = {
        f["id"]: f for f in facilities if "unavailable" in f["status"].lower()
    }

    newly_available   = []
    still_unavailable = []

    for fid, fdata in previous_state.items():
        if fdata.get("status", "").lower() == "unavailable":
            if fid not in current_unavailable:
                newly_available.append(fdata)   # was unavailable, now it's not
            else:
                still_unavailable.append(fdata)

    # First run – seed the state, notify about all currently unavailable
    if not previous_state:
        log.info("First run – recording baseline state.")
        if current_unavailable:
            msg = build_slack_message([], list(current_unavailable.values()))
            log.info("Posting initial unavailability snapshot to Slack.")
            post_slack(msg)
    else:
        if newly_available or still_unavailable:
            msg = build_slack_message(newly_available, still_unavailable)
            post_slack(msg)
        else:
            log.info("No status changes detected.")

    # ── Persist current state ─────────────────────────────────────────────────
    new_state = {f["id"]: f for f in facilities}
    save_state(new_state)
    log.info("State saved. Run complete.")


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Wing facility availability monitor")
    parser.add_argument(
        "--loop",
        action="store_true",
        help="Keep running, polling every POLL_HOURS hours",
    )
    args = parser.parse_args()

    if args.loop:
        log.info("Starting hourly monitor loop (Ctrl+C to stop) …")
        while True:
            try:
                run_check()
            except Exception as exc:
                log.exception("Error during check: %s", exc)
            log.info("Sleeping %d hour(s) …", POLL_HOURS)
            time.sleep(POLL_HOURS * 3600)
    else:
        run_check()


if __name__ == "__main__":
    main()
