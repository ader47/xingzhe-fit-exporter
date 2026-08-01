#!/usr/bin/env python3
"""Create a safe, local list of FIT files missing from Komoot.

The script reads completed activities with the user's normal signed-in browser
session, then matches their start timestamps to the original GPX files. It does
not upload, edit, or delete anything in Komoot.
"""
from __future__ import annotations

import argparse
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from playwright.sync_api import sync_playwright

KOMOOT = "https://www.komoot.com/"


def parse_time(value: str | int | float | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / (1000 if value > 10**11 else 1), timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def gpx_start(path: Path) -> datetime | None:
    root = ET.parse(path).getroot()
    value = root.findtext(".//{*}trkpt/{*}time")
    return parse_time(value)


def tours_from(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    embedded = payload.get("_embedded", {})
    for key in ("tours", "items", "activities"):
        if isinstance(embedded, dict) and isinstance(embedded.get(key), list):
            return embedded[key]
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def tour_start(tour: dict) -> datetime | None:
    for key in ("date", "start_date", "start_time", "timestamp", "time"):
        value = tour.get(key)
        result = parse_time(value)
        if result:
            return result
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Find locally exported FIT files missing from Komoot.")
    parser.add_argument("--user-id", required=True, help="numeric ID in your komoot.com/user/USER_ID profile URL")
    parser.add_argument("--fit-folder", type=Path, default=Path("fit-upload"))
    parser.add_argument("--source-folder", type=Path, default=Path("all-activities"),
                        help="folder holding ACTIVITY_ID/ACTIVITY_ID.gpx exports")
    parser.add_argument("--out", type=Path, default=Path("komoot-reconciliation"))
    parser.add_argument("--profile", type=Path, default=Path(".komoot-browser-profile"))
    parser.add_argument("--tolerance", type=int, default=180,
                        help="timestamp matching tolerance in seconds (default: 180)")
    parser.add_argument("--no-prompt", action="store_true")
    args = parser.parse_args()
    fits = sorted(args.fit_folder.glob("*.fit"))
    if not fits:
        parser.error(f"no FIT files in {args.fit_folder}")

    args.out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch_persistent_context(str(args.profile), headless=False)
        page = browser.pages[0]
        page.goto(KOMOOT, wait_until="domcontentloaded")
        if not args.no_prompt:
            input("Log in to Komoot in the browser if needed, then press Enter. ")
        tours: list[dict] = []
        for number in range(100):
            path = f"/api/v007/users/{args.user_id}/tours/?verified=true&limit=100&page={number}"
            payload = page.evaluate("""async path => {
                const response = await fetch(path, {credentials: 'include'});
                if (!response.ok) throw new Error(`${response.status} ${await response.text()}`);
                return await response.json();
            }""", path)
            current = tours_from(payload)
            tours.extend(current)
            page_info = payload.get("page", {}) if isinstance(payload, dict) else {}
            if not current or number + 1 >= int(page_info.get("totalPages", number + 1)):
                break
        browser.close()

    starts: list[datetime] = []
    for tour in tours:
        start = tour_start(tour)
        if start:
            starts.append(start)
    missing, matched, unknown = [], [], []
    for fit in fits:
        ride_id = fit.stem
        gpx = args.source_folder / ride_id / f"{ride_id}.gpx"
        if not gpx.exists():
            unknown.append({"fit": str(fit), "reason": "matching GPX not found"})
            continue
        start = gpx_start(gpx)
        if start is None:
            unknown.append({"fit": str(fit), "reason": "GPX has no start time"})
            continue
        if any(abs((start - remote).total_seconds()) <= args.tolerance for remote in starts):
            matched.append({"fit": str(fit), "start": start.isoformat()})
        else:
            missing.append({"fit": str(fit), "start": start.isoformat()})

    missing_dir = args.out / "missing-fits"
    missing_dir.mkdir(exist_ok=True)
    for row in missing:
        source = Path(row["fit"])
        shutil.copy2(source, missing_dir / source.name)
    report = {"local_fits": len(fits), "komoot_tours_read": len(tours),
              "matched": matched, "missing": missing, "unknown": unknown}
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Komoot tours read: {len(tours)}")
    print(f"Matched locally: {len(matched)}")
    print(f"Missing FIT files: {len(missing)}")
    print(f"Missing FIT folder: {missing_dir}")
    if unknown:
        print(f"Unmatched due to missing source data: {len(unknown)}")


if __name__ == "__main__":
    main()
