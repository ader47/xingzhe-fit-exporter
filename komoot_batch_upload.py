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
UPLOAD_FILE = re.compile(r"^(upload file|上传文件)$", re.I)
AS_ACTIVITY = re.compile(r"(import as activity|作为活动导入|导入为活动)", re.I)
NEXT = re.compile(r"^(next|下一步)$", re.I)
FINAL_IMPORT = re.compile(r"^(import activity|import|导入活动|导入)$", re.I)
PUBLIC = re.compile(r"^(public|everyone|anyone|公开|所有人可见)$", re.I)
PRIVATE = re.compile(r"^(private|only me|only you|仅自己|私密)$", re.I)


def click_first(page: Page, pattern: re.Pattern[str], timeout: int = 5000) -> bool:
    """Click whichever Komoot control appears, without serial 5-second waits.

    The activity choice is a card (plain text), while Next and Import Activity
    are buttons.  Probing each kind with a full timeout made every import wait
    10--20 seconds after the page had visibly loaded.
    """
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for locator in (
            page.get_by_role("button", name=pattern),
            page.get_by_role("link", name=pattern),
            page.get_by_text(pattern, exact=True),
        ):
            try:
                if locator.count() and locator.first.is_visible():
                    locator.first.click(timeout=400)
                    return True
            except PlaywrightTimeoutError:
                pass
        page.wait_for_timeout(100)
    return False


def file_input(page: Page, timeout: int = 1000):
    locator = page.locator('input[type="file"]')
    locator.first.wait_for(state="attached", timeout=timeout)
    return locator.first


def upload_file_input(page: Page):
    """Find the upload input; click Komoot's real upload button if needed."""
    try:
        return file_input(page)
    except PlaywrightTimeoutError:
        if not click_first(page, UPLOAD_FILE):
            raise RuntimeError("Komoot did not show the Upload File control.")
        return file_input(page)


def choose_activity(page: Page) -> bool:
    """Select the actual label/card shown in the current import flow."""
    label = page.locator("label").filter(has_text=AS_ACTIVITY)
    try:
        if label.count() and label.first.is_visible():
            label.first.click(timeout=600)
            return True
    except PlaywrightTimeoutError:
        pass
    return click_first(page, AS_ACTIVITY, timeout=10000)


def set_privacy(page: Page, privacy: str) -> None:
    """Set activity visibility without disturbing Komoot's default private state."""
    body = page.locator("body").inner_text()
    # Komoot's default value is "Only you". There is no radio control until
    # the user presses the fourth (visibility) Change button, so keep the
    # default for private imports rather than treating it as an error.
    if privacy == "private" and re.search(r"(visibility:\s*(only you|only me)|可见性.*(仅自己|私密))", body, re.I):
        return
    if privacy == "public" and re.search(r"(visibility:\s*(anyone|everyone|public)|可见性.*公开)", body, re.I):
        return
    changes = page.get_by_text(re.compile(r"^(change|更改)$", re.I), exact=True)
    try:
        # The visibility card is the last Change button on the import form.
        changes.last.click(timeout=3000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("Could not open Komoot's Activity visibility setting.") from exc
    desired = PUBLIC if privacy == "public" else PRIVATE
    label = page.locator("label").filter(has_text=desired)
    try:
        if label.count() and label.first.is_visible():
            label.first.click(timeout=800)
            return
    except PlaywrightTimeoutError:
        pass
    for role in ("radio", "button"):
        try:
            page.get_by_role(role, name=desired).first.click(timeout=4000)
            return
        except PlaywrightTimeoutError:
            pass
    try:
        page.get_by_text(desired, exact=False).first.click(timeout=4000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"Could not find the Komoot {privacy!r} privacy control.") from exc


def upload_one(page: Page, import_page_url: str, fit: Path, privacy: str, settle: float) -> None:
    """Upload one activity from the already-confirmed Completed activities page."""
    page.goto(import_page_url, wait_until="domcontentloaded")
    upload_file_input(page).set_input_files(str(fit.resolve()))
    if not choose_activity(page):
        raise RuntimeError("Komoot did not show the 'Import as Activity' step.")
    if not click_first(page, NEXT, timeout=5000):
        raise RuntimeError("Komoot did not show the Next button after selecting 'Import as Activity'.")
    set_privacy(page, privacy)
    if not click_first(page, FINAL_IMPORT, timeout=10000):
        raise RuntimeError("Komoot did not show the final Import Activity button.")
    # The click has submitted the import. Keep a short buffer before opening
    # the next upload page, without waiting for the rendered activity map.
    page.wait_for_timeout(max(0, settle) * 1000)


def main() -> None:
    parser = argparse.ArgumentParser(description="Import all FIT files in a folder into Komoot.")
    parser.add_argument("--folder", type=Path, default=Path("fit-upload"))
    parser.add_argument("--privacy", choices=("private", "public"), default="private")
    parser.add_argument("--profile", type=Path, default=Path(".komoot-browser-profile"))
    parser.add_argument("--delay", type=float, default=0.0,
                        help="seconds between imports (default: 0)")
    parser.add_argument("--settle", type=float, default=0.3,
                        help="seconds after clicking Import Activity (default: 0.3)")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--no-prompt", action="store_true")
    parser.add_argument("--import-url", default=KOMOOT_UPLOAD,
                        help="Komoot upload page (default: https://www.komoot.com/upload)")
    parser.add_argument("--debug-dir", type=Path, default=Path(".komoot-debug"),
                        help="local screenshots and trace for diagnosing an import failure")
    parser.add_argument("--debug", action="store_true",
                        help="record a full Playwright trace (slow; use only when diagnosing failures)")
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
        args.debug_dir.mkdir(parents=True, exist_ok=True)
        if args.debug:
            browser.tracing.start(screenshots=True, snapshots=True, sources=True)
        try:
            page = browser.pages[0]
            page.goto(KOMOOT, wait_until="domcontentloaded")
            if not args.no_prompt:
                input("Log in to Komoot in the browser, then press Enter here. ")
            import_page_url = args.import_url
            # Validate the page before sending any FIT data.
            page.goto(import_page_url, wait_until="domcontentloaded")
            try:
                upload_file_input(page)
            except PlaywrightTimeoutError:
                raise RuntimeError("Komoot's upload page did not show an import control after login. "
                                   "Confirm that you are signed in, then retry.")
            for index, fit in enumerate(files, 1):
                try:
                    upload_one(page, import_page_url, fit, args.privacy, args.settle)
                    result = {"file": str(fit), "status": "ok", "privacy": args.privacy}
                    print(f"[{index}/{len(files)}] {fit.name}: imported")
                except Exception as exc:
                    stamp = time.strftime("%Y%m%d-%H%M%S")
                    screenshot = args.debug_dir / f"failure-{stamp}.png"
                    details = args.debug_dir / f"failure-{stamp}.txt"
                    page.screenshot(path=str(screenshot), full_page=True)
                    details.write_text(f"URL: {page.url}\n\n{page.locator('body').inner_text()}")
                    result = {"file": str(fit), "status": "error", "error": str(exc),
                              "screenshot": str(screenshot), "details": str(details)}
                    print(f"[{index}/{len(files)}] {fit.name}: ERROR {exc}", file=sys.stderr)
                    print(f"Saved diagnostic files in {args.debug_dir}", file=sys.stderr)
                    with manifest.open("a") as output:
                        output.write(json.dumps(result, ensure_ascii=False) + "\n")
                    break
                with manifest.open("a") as output:
                    output.write(json.dumps(result, ensure_ascii=False) + "\n")
                time.sleep(max(0, args.delay))
        finally:
            if args.debug:
                browser.tracing.stop(path=str(args.debug_dir / "komoot-upload-trace.zip"))
            browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
