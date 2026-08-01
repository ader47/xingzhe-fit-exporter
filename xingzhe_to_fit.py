#!/usr/bin/env python3
"""Export Xingzhe activities to FIT using your own interactive browser login."""
from __future__ import annotations

import argparse, json, sys, time
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

from garmin_fit_sdk import Encoder, Profile
from playwright.sync_api import sync_playwright

BASE = "https://www.imxingzhe.com"

def gpx_points(data: bytes):
    root = ET.fromstring(data)
    pts = []
    for p in root.findall(".//{*}trkpt"):
        t = p.findtext("{*}time")
        if not t: continue
        pts.append({"lat": float(p.attrib["lat"]), "lon": float(p.attrib["lon"]),
                    "alt": float(p.findtext("{*}ele") or 0), "time": parse_time(t)})
    return pts

def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

def num(value, default=None):
    try: return float(value)
    except (TypeError, ValueError): return default

def stream_points(payload, fallback):
    """Normalize Xingzhe stream payloads; their field names have changed over time."""
    data = payload.get("data", payload)
    if isinstance(data, dict) and "data" in data: data = data["data"]
    if isinstance(data, list): records = data
    elif isinstance(data, dict):
        keys = ("records", "points", "stream", "track")
        records = next((data[k] for k in keys if isinstance(data.get(k), list)), None)
        if records is None:
            locations, alts, speeds, stamps = (data.get(k, []) for k in ("location", "altitude", "speed", "timestamp"))
            records = [{"location": locations[i] if i < len(locations) else None,
                        "altitude": alts[i] if i < len(alts) else None,
                        "speed": speeds[i] if i < len(speeds) else None,
                        "timestamp": stamps[i] if i < len(stamps) else None}
                       for i in range(max(len(locations), len(fallback)))]
    else: records = []
    out = []
    for i, r in enumerate(records):
        f = fallback[min(i, len(fallback)-1)] if fallback else None
        loc = r.get("location") or r.get("lnglat") or r.get("coordinate")
        lon = num(r.get("longitude") or r.get("lon")); lat = num(r.get("latitude") or r.get("lat"))
        if isinstance(loc, (list, tuple)) and len(loc) >= 2: lon, lat = num(loc[0]), num(loc[1])
        if (lat is None or lon is None) and f: lat, lon = f["lat"], f["lon"]
        stamp = r.get("timestamp") or r.get("time")
        if isinstance(stamp, (int, float)): stamp = datetime.fromtimestamp(stamp / (1000 if stamp > 10**11 else 1), timezone.utc)
        elif isinstance(stamp, str): stamp = parse_time(stamp)
        elif f: stamp = f["time"]
        if lat is None or lon is None or stamp is None: continue
        speed = num(r.get("speed") or r.get("velocity"))
        # Xingzhe chart speed is km/h; FIT record speed is m/s.
        if speed is not None: speed /= 3.6
        out.append({"lat":lat,"lon":lon,"alt":num(r.get("altitude") or r.get("ele"), f["alt"] if f else 0),"speed":speed,"time":stamp})
    return out

def semicircles(deg): return int(deg * (2**31) / 180)

def write_fit(points, output):
    if not points: raise RuntimeError("No usable GPS samples were returned.")
    enc = Encoder()
    def write(kind, values):
        values["mesg_num"] = Profile["mesg_num"][kind]
        enc.write_mesg(values)
    write("FILE_ID", {"type":"activity", "manufacturer":"development", "product":1,
                      "time_created":points[0]["time"]})
    for p in points:
        msg = {"timestamp":p["time"], "position_lat":semicircles(p["lat"]), "position_long":semicircles(p["lon"]),
               "altitude":p["alt"], "enhanced_altitude":p["alt"]}
        if p["speed"] is not None: msg.update({"speed":p["speed"], "enhanced_speed":p["speed"]})
        write("RECORD", msg)
    duration = (points[-1]["time"] - points[0]["time"]).total_seconds()
    write("SESSION", {"timestamp":points[-1]["time"], "start_time":points[0]["time"],
                      "total_elapsed_time":duration, "total_timer_time":duration,
                      "sport":"cycling", "sub_sport":"generic", "event":"session", "event_type":"stop"})
    write("ACTIVITY", {"timestamp":points[-1]["time"], "total_timer_time":duration,
                       "num_sessions":1, "type":"manual", "event":"activity", "event_type":"stop"})
    output.write_bytes(enc.close())

def api(page, path):
    for attempt in range(6):
        try:
            return page.evaluate("""async p => { const r=await fetch(p,{credentials:'include'}); const t=await r.text();
              if(!r.ok) throw new Error(r.status+' '+t); try{return JSON.parse(t)}catch{return t} }""", path)
        except Exception as exc:
            if "request limit exceeded" not in str(exc) or attempt == 5:
                raise
            time.sleep(1.2 * (attempt + 1))

def activity_ids(payload):
    """Extract activity IDs from either the legacy web API or OpenAPI pagination shape."""
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        rows = data["data"]
    else:
        rows = data.get("results") or data.get("items") or data.get("workouts") or data.get("activities") or []
    return [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id") is not None]

def list_activities(page, page_size):
    """List every activity through the signed-in Xingzhe web session."""
    ids, offset = [], 0
    while True:
        payload = api(page, f"/api/v1/pgworkout/?&offset={offset}&limit={page_size}&sport=&year=&month=")
        page_ids = activity_ids(payload)
        if not page_ids: break
        ids.extend(page_ids)
        if len(page_ids) < page_size: break
        offset += page_size
    return list(dict.fromkeys(ids))

def export_ride(page, ride_id, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    gpx=api(page, f"/api/v1/pgworkout/{ride_id}/gpx/")
    if not isinstance(gpx, str): raise RuntimeError("GPX download did not return text.")
    gpx_bytes=gpx.encode(); raw=api(page, f"/api/v1/pgworkout/{ride_id}/stream/")
    (out_dir/f"{ride_id}.gpx").write_bytes(gpx_bytes)
    (out_dir/f"{ride_id}.stream.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    points=stream_points(raw, gpx_points(gpx_bytes)); target=out_dir/f"{ride_id}.fit"
    write_fit(points, target)
    return target, len(points)

def main():
    ap=argparse.ArgumentParser(description="Export Xingzhe rides to FIT.")
    ap.add_argument("ride_id", nargs="?", help="numeric Xingzhe activity ID")
    ap.add_argument("--out", type=Path, default=Path("fit-output"))
    ap.add_argument("--profile", type=Path, default=Path(".xingzhe-browser-profile"))
    ap.add_argument("--all", action="store_true", help="export every activity in the signed-in account")
    ap.add_argument("--page-size", type=int, default=20, choices=range(1, 21), metavar="1..20")
    ap.add_argument("--overwrite", action="store_true", help="re-export activities that already have a FIT file")
    ap.add_argument("--no-prompt", action="store_true", help="continue immediately using an existing logged-in browser profile")
    args=ap.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    if bool(args.ride_id) == bool(args.all):
        ap.error("pass one ride_id, or use --all")
    with sync_playwright() as pw:
        browser=pw.chromium.launch_persistent_context(str(args.profile), headless=False)
        page=browser.pages[0]; page.goto(BASE)
        if not args.no_prompt:
            input("Log in in the browser if needed, then press Enter here. ")
        ride_ids = [args.ride_id] if args.ride_id else list_activities(page, args.page_size)
        if not ride_ids: raise RuntimeError("No activities were found. Confirm that the browser is logged in.")
        print(f"Found {len(ride_ids)} activities.")
        manifest = args.out / "batch-manifest.jsonl"
        for index, ride_id in enumerate(ride_ids, 1):
            ride_out = args.out if args.ride_id else args.out / ride_id
            target = ride_out / f"{ride_id}.fit"
            if target.exists() and not args.overwrite:
                print(f"[{index}/{len(ride_ids)}] {ride_id}: already exported; skipping")
                continue
            try:
                target, count = export_ride(page, ride_id, ride_out)
                outcome = {"id": ride_id, "status": "ok", "fit": str(target), "records": count}
                print(f"[{index}/{len(ride_ids)}] {ride_id}: wrote {count} records")
            except Exception as exc:
                outcome = {"id": ride_id, "status": "error", "error": str(exc)}
                print(f"[{index}/{len(ride_ids)}] {ride_id}: ERROR {exc}", file=sys.stderr)
            with manifest.open("a") as f: f.write(json.dumps(outcome, ensure_ascii=False) + "\n")
            time.sleep(.2)
        browser.close()

if __name__ == "__main__":
    try: main()
    except Exception as e: print(f"Error: {e}", file=sys.stderr); sys.exit(1)
