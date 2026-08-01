#!/usr/bin/env python3
"""Import a folder of FIT activities into Komoot through its normal web UI."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

KOMOOT = "https://www.komoot.com/"
KOMOOT_UPLOAD = "https://www.komoot.com/upload"
# Komoot's current activity page shortens this button to simply "Import" in
# some layouts; the older layout says "Import a GPS file".
IMPORT_LABEL = re.compile(r"^(import|导入)$|(import.*gps|gps.*import|导入.*gps|导入.*文件)", re.I)
AS_ACTIVITY = re.compile(r"(import as activity|作为活动导入|导入为活动)", re.I)
FINAL_IMPORT = re.compile(r"^(import activity|import|导入活动|导入)$", re.I)
PUBLIC = re.compile(r"^(public|everyone|公开|所有人可见)$", re.I)
PRIVATE = re.compile(r"^(private|only me|仅自己|私密)$", re.I)


def click_first(page: Page, pattern: re.Pattern[str], timeout: int = 5000) -> bool:
    for locator in (page.get_by_role("button", name=pattern), page.get_by_role("link", name=pattern)):
        try:
            locator.first.click(timeout=timeout)
            return True
        except PlaywrightTimeoutError:
            pass
    return False


def file_input(page: Page):
    locator = page.locator('input[type="file"]')
    locator.first.wait_for(state="attached", timeout=8000)
    return locator.first


def set_privacy(page: Page, privacy: str) -> None:
    """Pick privacy, failing safely if the current Komoot dialog is unfamiliar."""
    desired = PUBLIC if privacy == "public" else PRIVATE
    for role in ("radio", "button"):
        try:
            page.get_by_role(role, name=desired).first.click(timeout=2500)
            return
        except PlaywrightTimeoutError:
            pass
    try:
        page.get_by_text(desired, exact=False).first.click(timeout=2500)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"Could not find the Komoot {privacy!r} privacy control.") from exc


def upload_one(page: Page, import_page_url: str, fit: Path, privacy: str) -> None:
    """Upload one activity from the already-confirmed Completed activities page."""
    page.goto(import_page_url, wait_until="domcontentloaded")
    if not click_first(page, IMPORT_LABEL):
        try:
            file_input(page)
        except PlaywrightTimeoutError as exc:
            raise RuntimeError("Could not find Komoot's 'Import a GPS file' control. "
                               "Check that you are signed in and use Komoot in English.") from exc
    file_input(page).set_input_files(str(fit.resolve()))
    if not click_first(page, AS_ACTIVITY, timeout=10000):
        raise RuntimeError("Komoot did not show the 'Import as Activity' step.")
    set_privacy(page, privacy)
    if not click_first(page, FINAL_IMPORT, timeout=10000):
        raise RuntimeError("Komoot did not show the final Import Activity button.")
    page.wait_for_timeout(1200)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import all FIT files in a folder into Komoot.")
    parser.add_argument("--folder", type=Path, default=Path("fit-upload"))
    parser.add_argument("--privacy", choices=("private", "public"), default="private")
    parser.add_argument("--profile", type=Path, default=Path(".komoot-browser-profile"))
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-prompt", action="store_true")
    parser.add_argument("--import-url", default=KOMOOT_UPLOAD,
                        help="Komoot upload page (default: https://www.komoot.com/upload)")
    args = parser.parse_args()

    files = sorted(args.folder.expanduser().glob("*.fit"))
    if args.limit is not None:
        files = files[:args.limit]
    if not files:
        parser.error(f"no .fit files found in {args.folder}")
    manifest = args.folder / "komoot-upload-manifest.jsonl"
    completed: set[str] = set()
    if args.resume and manifest.exists():
        for line in manifest.read_text().splitlines():
            try:
                record = json.loads(line)
                if record.get("status") == "ok":
                    completed.add(record["file"])
            except (json.JSONDecodeError, KeyError):
                continue
    files = [f for f in files if str(f) not in completed]
    if not files:
        print("All FIT files are already marked as uploaded.")
        return

    print(f"Ready to import {len(files)} FIT file(s) with privacy={args.privacy}.")
    print("Komoot will receive these activities. Review the setting before continuing.")
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(str(args.profile), headless=False)
        page = browser.pages[0]
        page.goto(KOMOOT, wait_until="domcontentloaded")
        if not args.no_prompt:
            input("Log in to Komoot in the browser, then press Enter here. ")
        import_page_url = args.import_url
        # Validate the page before sending any FIT data. This avoids mistaking
        # Komoot's home page for an import form.
        page.goto(import_page_url, wait_until="domcontentloaded")
        if not click_first(page, IMPORT_LABEL):
            browser.close()
            raise RuntimeError("Komoot's upload page did not show an import control after login. "
                               "Confirm that you are signed in, then retry.")
        for index, fit in enumerate(files, 1):
            try:
                upload_one(page, import_page_url, fit, args.privacy)
                result = {"file": str(fit), "status": "ok", "privacy": args.privacy}
                print(f"[{index}/{len(files)}] {fit.name}: imported")
            except Exception as exc:
                result = {"file": str(fit), "status": "error", "error": str(exc)}
                print(f"[{index}/{len(files)}] {fit.name}: ERROR {exc}", file=sys.stderr)
                with manifest.open("a") as output:
                    output.write(json.dumps(result, ensure_ascii=False) + "\n")
                break
            with manifest.open("a") as output:
                output.write(json.dumps(result, ensure_ascii=False) + "\n")
            time.sleep(max(0, args.delay))
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
