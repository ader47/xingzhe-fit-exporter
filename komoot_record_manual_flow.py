#!/usr/bin/env python3
"""Record the visible controls used during one manual Komoot import.

This is a local diagnostic aid. It records clicks on buttons, links and file
pickers, but deliberately never records typed input, passwords, cookies, HTTP
headers or file contents.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

UPLOAD_URL = "https://www.komoot.com/upload"


def main() -> None:
    parser = argparse.ArgumentParser(description="Record one manual Komoot import flow locally.")
    parser.add_argument("--profile", type=Path, default=Path(".komoot-browser-profile"))
    parser.add_argument("--out", type=Path, default=Path(".komoot-manual-recording.jsonl"))
    args = parser.parse_args()

    with args.out.open("a", encoding="utf-8") as output:
        def record(event: dict) -> None:
            event["recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            output.write(json.dumps(event, ensure_ascii=False) + "\n")
            output.flush()

        with sync_playwright() as pw:
            context = pw.chromium.launch_persistent_context(str(args.profile), headless=False)
            context.expose_binding("recordKomootClick", lambda source, event: record(event))
            context.add_init_script("""
                (() => {
                  document.addEventListener('click', event => {
                    const item = event.target.closest('button,a,[role="button"],label,input[type="file"]');
                    if (!item) return;
                    // Do not inspect or transmit input values. Text and labels
                    // identify the UI control sufficiently for automation.
                    window.recordKomootClick({
                      type: 'click',
                      url: location.href,
                      tag: item.tagName.toLowerCase(),
                      role: item.getAttribute('role'),
                      text: (item.innerText || item.getAttribute('aria-label') || item.getAttribute('title') || '')
                        .replace(/\\s+/g, ' ').trim().slice(0, 180),
                      aria_label: item.getAttribute('aria-label'),
                    });
                  }, true);
                })();
            """)
            page = context.pages[0]
            page.on("framenavigated", lambda frame: record({"type": "navigation", "url": frame.url})
                    if frame == page.main_frame else None)
            page.goto(UPLOAD_URL, wait_until="domcontentloaded")
            print("Log in if needed, then manually import ONE test FIT activity in the browser.")
            input("After the activity is fully imported, press Enter here to finish recording. ")
            page.screenshot(path=str(args.out.with_suffix(".png")), full_page=True)
            record({"type": "finished", "url": page.url})
            context.close()
    print(f"Saved click log to {args.out} and final screenshot next to it.")


if __name__ == "__main__":
    main()
