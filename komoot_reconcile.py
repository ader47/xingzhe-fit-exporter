#!/usr/bin/env python3
"""Create a safe, local list of FIT files missing from Komoot.

The script reads completed activities with the user's normal signed-in browser
session, then matches their start timestamps to the original GPX files. It does
not upload, edit, or delete anything in Komoot.
"""
from __future__ import annotations

import argparse
import json
import math
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


def haversine_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two WGS-84 points."""
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def gpx_signature(path: Path) -> dict | None:
    """Return start/end and recorded-track distance for one GPX file."""
    root = ET.parse(path).getroot()
    points = root.findall(".//{*}trkpt")
    if not points:
        return None
    try:
        coords = [(float(point.attrib["lat"]), float(point.attrib["lon"])) for point in points]
    except (KeyError, ValueError):
        return None
    distance = sum(
        haversine_meters(lat1, lon1, lat2, lon2)
        for (lat1, lon1), (lat2, lon2) in zip(coords, coords[1:])
    )
    return {"start": coords[0], "end": coords[-1], "distance": distance}


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


def route_candidates(signature: dict, tours: list[dict]) -> list[tuple[float, int]]:
    """Find Komoot tours that plausibly describe this recorded GPX route.

    Komoot assigns a newly-imported activity its import time in ``date``.  The
    durable comparison fields on the activity-list endpoint are the start
    coordinate and total distance, so use both and retain only tight matches.
    """
    start_lat, start_lon = signature["start"]
    local_distance = signature["distance"]
    candidates: list[tuple[float, int]] = []
    for index, tour in enumerate(tours):
        point = tour.get("start_point")
        remote_distance = tour.get("distance")
        if not isinstance(point, dict) or not isinstance(remote_distance, (int, float)):
            continue
        try:
            start_gap = haversine_meters(start_lat, start_lon, float(point["lat"]), float(point["lng"]))
        except (KeyError, TypeError, ValueError):
            continue
        distance_gap = abs(local_distance - remote_distance)
        # A GPS point can drift slightly and Komoot may simplify the line, but
        # larger differences are ambiguous rather than safe matches.
        if start_gap <= 250 and distance_gap <= max(250, local_distance * 0.03):
            score = start_gap + distance_gap
            candidates.append((score, index))
    return sorted(candidates)


def globally_match_routes(signatures: list[dict], tours: list[dict]) -> dict[int, tuple[float, int]]:
    """Find a maximum-cardinality one-to-one set of local/remote route pairs.

    Several rides can leave the same location with a similar distance.  A
    nearest-first assignment can then strand an otherwise valid route.  The
    augmenting-path assignment below maximizes the number of matches while
    retaining the score-ordered candidates for deterministic tie-breaking.
    """
    candidates = [route_candidates(signature, tours) for signature in signatures]
    remote_to_local: dict[int, tuple[int, float]] = {}

    def assign(local_index: int, visited: set[int]) -> bool:
        for score, remote_index in candidates[local_index]:
            if remote_index in visited:
                continue
            visited.add(remote_index)
            previous = remote_to_local.get(remote_index)
            if previous is None or assign(previous[0], visited):
                remote_to_local[remote_index] = (local_index, score)
                return True
        return False

    # Routes with fewer alternatives go first, reducing arbitrary tie effects.
    for local_index in sorted(range(len(signatures)), key=lambda index: len(candidates[index])):
        assign(local_index, set())
    return {local_index: (score, remote_index)
            for remote_index, (local_index, score) in remote_to_local.items()}


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
            # Do not use ``verified=true`` here.  That endpoint filter is for
            # verified/planned tours and excludes FIT activities imported by
            # the user.  Fetch the user's full tour history instead; the exact
            # start-time match below identifies the local completed activities.
            path = f"/api/v007/users/{args.user_id}/tours/?limit=100&page={number}"
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

    # Imported activities receive Komoot's import date, not the original ride
    # date.  Build all route signatures before making global one-to-one matches.
    missing, matched, unknown = [], [], []
    usable: list[tuple[Path, dict]] = []
    for fit in fits:
        ride_id = fit.stem
        gpx = args.source_folder / ride_id / f"{ride_id}.gpx"
        if not gpx.exists():
            unknown.append({"fit": str(fit), "reason": "matching GPX not found"})
            continue
        signature = gpx_signature(gpx)
        if signature is None:
            unknown.append({"fit": str(fit), "reason": "GPX has no usable track points"})
            continue
        usable.append((fit, signature))

    chosen = globally_match_routes([signature for _, signature in usable], tours)
    for local_index, (fit, signature) in enumerate(usable):
        if local_index in chosen:
            score, index = chosen[local_index]
            matched.append({"fit": str(fit), "tour_id": tours[index].get("id"), "score_m": round(score, 1)})
        else:
            missing.append({"fit": str(fit), "distance_m": round(signature["distance"], 1)})

    # Keep the generated upload folder an exact reflection of this run.  A
    # previous report may have contained files that are no longer missing.
    missing_dir = args.out / "missing-fits"
    missing_dir.mkdir(exist_ok=True)
    for old_fit in missing_dir.glob("*.fit"):
        old_fit.unlink()
    for row in missing:
        source = Path(row["fit"])
        shutil.copy2(source, missing_dir / source.name)
    report = {"local_fits": len(fits), "komoot_tours_read": len(tours),
              "matched": matched, "missing": missing, "unknown": unknown}
    (args.out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    # Useful for diagnosing future Komoot API changes.  This remains local:
    # the reconciliation directory is intentionally ignored by Git.
    (args.out / "komoot-tours-raw.json").write_text(
        json.dumps(tours, ensure_ascii=False, indent=2)
    )
    print(f"Komoot tours read: {len(tours)}")
    print(f"Matched locally: {len(matched)}")
    print(f"Missing FIT files: {len(missing)}")
    print(f"Missing FIT folder: {missing_dir}")
    if unknown:
        print(f"Unmatched due to missing source data: {len(unknown)}")


if __name__ == "__main__":
    main()
