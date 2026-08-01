# Xingzhe → FIT exporter

Exports one Xingzhe activity automatically after you log in in the browser window it opens. It saves the original GPX, the raw speed-stream response, and a generated FIT file for auditing.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

## Run

Open a Xingzhe activity page; its final numeric URL component is the ride ID. Then run:

```bash
.venv/bin/python xingzhe_to_fit.py RIDE_ID --out fit-output
```

Log in yourself in the browser window. The app never asks for or stores a password. Review the produced `.fit` alongside its `.gpx` and `.stream.json` in a FIT viewer before importing it elsewhere.

## Export every activity

To export the whole signed-in account, use batch mode:

```bash
.venv/bin/python xingzhe_to_fit.py --all --out all-activities
```

Each activity is stored in its own `all-activities/ACTIVITY_ID/` folder. `batch-manifest.jsonl` records successes and errors. Re-running the same command skips completed FIT files; add `--overwrite` to recreate them.

If the saved browser profile is already signed in, add `--no-prompt` when running through a non-interactive launcher such as `conda run`.



## Current scope

The exporter implements a single activity first, intentionally: this lets you validate timestamp, speed, and elevation mapping before a bulk export is added.
