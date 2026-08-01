# Xingzhe → FIT exporter

Exports one Xingzhe activity automatically after you log in in the browser window it opens. It saves the original GPX, the raw speed-stream response, and a generated FIT file for auditing.

## Install (your Anaconda base environment)

```bash
conda activate base
pip install -r requirements.txt
playwright install chromium
```

## Run

Open a Xingzhe activity page; its final numeric URL component is the ride ID. Then run:

```bash
python xingzhe_to_fit.py RIDE_ID --out fit-output
```

Log in yourself in the browser window. The app never asks for or stores a password. Review the produced `.fit` alongside its `.gpx` and `.stream.json` in a FIT viewer before importing it elsewhere.

## Export every activity

To export the whole signed-in account, use batch mode:

```bash
python xingzhe_to_fit.py --all --out all-activities
```

Each activity is stored in its own `all-activities/ACTIVITY_ID/` folder. `batch-manifest.jsonl` records successes and errors. Re-running the same command skips completed FIT files; add `--overwrite` to recreate them.

If the saved browser profile is already signed in, add `--no-prompt` when running through a non-interactive launcher such as `conda run`.



## Import all FIT files into Komoot

Komoot's website imports completed activities one file at a time. This script
drives that normal web flow in sequence: it opens a browser, you log in
yourself, and it uploads every `.fit` file from one folder. Passwords are never
read or stored by the script.

Start with **private** visibility (recommended), verify a few imports, then use
public only if you want every route and its location history visible to anyone:

```bash
conda activate base
cd /Users/liufeng/Documents/Codex/2026-08-02/realtime-voice-chat/outputs/xingzhe-fit-exporter
python komoot_batch_upload.py --folder fit-upload --privacy private --limit 1
```

The opened browser starts on Komoot's home page. Log in, then press Enter in
the terminal. The script uses Komoot's dedicated `/upload` page for every
file. Remove `--limit 1` after the test. For public activities, replace
`private` with `public`. It saves `komoot-upload-manifest.jsonl` in the FIT
folder. If an upload stops, continue without repeating successes:

```bash
python komoot_batch_upload.py --folder fit-upload --privacy private --resume
```

The normal uploader adds no deliberate gap and waits only 0.3 seconds after
clicking Import Activity. Komoot still needs time to process each FIT file on
its servers; that server-side time cannot be bypassed safely.

The script stops at the first unfamiliar Komoot screen rather than continuing
with an unknown privacy setting.

If a test fails, it writes a screenshot and page text to `.komoot-debug/`.
Add `--debug` only when deeper diagnosis is needed; it records a full browser
trace and is substantially slower. These diagnostic files are local-only and
ignored by Git. Send the failure screenshot or text file to diagnose the
changed Komoot UI.

## Record one manual upload flow

If Komoot changes its UI, record one manual test import before changing the
uploader again:

```bash
python komoot_record_manual_flow.py
```

Log in and manually import one FIT file in the opened browser, then press Enter
in the terminal. It writes `.komoot-manual-recording.jsonl` and a final
screenshot locally. The log includes only clicked control labels and page URLs;
it does not capture typed text, passwords, cookies, headers, or file contents.

## Reconcile a partial upload

If an earlier version of the uploader marked files successful before Komoot had
created the activity, reconcile before uploading again. Open your Komoot profile
in a browser and copy the numeric ID from `komoot.com/user/USER_ID`, then run:

```bash
python komoot_reconcile.py --user-id USER_ID
```

After you log in, this reads your completed activities and matches their start
times to the local GPX exports. It writes a report and copies only missing FIT
files to `komoot-reconciliation/missing-fits/`. It does not change anything in
Komoot. Upload that folder using the normal uploader only after checking the
reported counts:

```bash
python komoot_batch_upload.py --folder komoot-reconciliation/missing-fits --privacy public --settle 0
```
