#!/usr/bin/env python3
"""
smartsheet_sync.py
------------------
Pushes the daily records (the same dict the Oura sync already builds) into a
Smartsheet "Daily Metrics" sheet, so a live Smartsheet dashboard can read it.

Behaviour:
  * First run on an empty sheet  -> adds every night it has (full back-fill).
  * Every run after that         -> adds any new nights, and refreshes the most
                                    recent SMARTSHEET_REFRESH_DAYS nights in case
                                    Oura revised them. Older rows are left alone.

Matches rows by the ISO date stored in the primary "Date" column.

Environment variables (set as GitHub secrets):
  SMARTSHEET_TOKEN              Smartsheet API access token
  SMARTSHEET_METRICS_SHEET_ID  numeric id of the Daily Metrics sheet
  SMARTSHEET_REFRESH_DAYS      recent days to re-update each run (default 14)

Weather keys (w_*) are included in the mapping now but simply skipped until the
weather step starts populating them — no change needed here when that lands.
"""

import os
import time
import datetime as dt
import requests

SS_TOKEN = os.environ.get("SMARTSHEET_TOKEN", "").strip()
SS_SHEET_ID = os.environ.get("SMARTSHEET_METRICS_SHEET_ID", "").strip()
SS_REFRESH_DAYS = int(os.environ.get("SMARTSHEET_REFRESH_DAYS", "14"))
SS_CHECKIN_ID = os.environ.get("SMARTSHEET_CHECKIN_SHEET_ID", "244139844128644").strip()
SS_BASE = "https://api.smartsheet.com/2.0"

DATE_TITLE = "Date"

# Smartsheet column title  ->  key in the daily record dict
FIELD_MAP = {
    "HRV": "avg_hrv",
    "HRV balance": "hrv_balance",
    "Deep (min)": "deep_min",
    "REM (min)": "rem_min",
    "Total sleep (h)": "total_sleep_h",
    "Efficiency %": "efficiency",
    "Latency (min)": "latency_min",
    "Resting HR": "resting_hr",
    "Temp dev (\u00b0F)": "temp_dev_f",
    "Resp rate": "resp_rate",
    "Sleep score": "sleep_score",
    "Readiness": "readiness_score",
    # weather (populated later; skipped while absent):
    "Pressure (mb)": "w_pressure",
    "Pressure \u0394 (mb)": "w_pressure_delta",
    "Humidity %": "w_humidity",
    "Rainfall (in)": "w_rain_in",
    "Sunshine (h)": "w_sunshine_h",
    "Outdoor temp (\u00b0F)": "w_temp_f",
}


def _headers():
    return {"Authorization": f"Bearer {SS_TOKEN}", "Content-Type": "application/json"}


def _request(method, path, payload=None, max_retries=6):
    url = f"{SS_BASE}{path}"
    for attempt in range(max_retries):
        r = requests.request(method, url, headers=_headers(), json=payload, timeout=60)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def _batch(rows, method):
    sent = 0
    for i in range(0, len(rows), 400):
        chunk = rows[i:i + 400]
        if not chunk:
            continue
        _request(method, f"/sheets/{SS_SHEET_ID}/rows", chunk)
        sent += len(chunk)
        time.sleep(0.3)
    return sent


def sync(records):
    if not (SS_TOKEN and SS_SHEET_ID):
        print("Smartsheet not configured; skipping Daily Metrics push.")
        return
    try:
        sheet = _request("GET", f"/sheets/{SS_SHEET_ID}").json()
    except Exception as e:
        print(f"Smartsheet read failed (rest of sync OK): {e}")
        return

    col_id = {c["title"]: c["id"] for c in sheet.get("columns", [])}
    if DATE_TITLE not in col_id:
        print("Smartsheet: 'Date' column not found; aborting push.")
        return
    date_col = col_id[DATE_TITLE]

    existing = {}
    for row in sheet.get("rows", []):
        for cell in row.get("cells", []):
            if cell.get("columnId") == date_col and cell.get("value") is not None:
                existing[str(cell["value"])] = row["id"]

    cutoff = (dt.date.today() - dt.timedelta(days=SS_REFRESH_DAYS)).isoformat()
    adds, updates = [], []

    for day in sorted(records):
        rec = records[day]
        data_cells = []
        for title, key in FIELD_MAP.items():
            if title not in col_id:
                continue
            val = rec.get(key)
            if val is None or val == "":
                continue
            data_cells.append({"columnId": col_id[title], "value": val})

        if day in existing:
            if day >= cutoff and data_cells:
                updates.append({"id": existing[day], "cells": data_cells})
        else:
            cells = [{"columnId": date_col, "value": day}] + data_cells
            adds.append({"toBottom": True, "cells": cells})

    added = _batch(adds, "POST") if adds else 0
    updated = _batch(updates, "PUT") if updates else 0
    print(f"Smartsheet Daily Metrics: {added} added, {updated} updated.")


# Daily Check-in form sheet: column title -> key in the daily record dict
CHECKIN_MAP = {
    "Mood": "c_mood",
    "Energy": "c_energy",
    "Comfort": "c_comfort",
    "Hands": "c_hands",
    "Body calm": "c_bodycalm",
    "Calm": "c_calm",
    "Clarity": "c_clarity",
    "Indoor humidity %": "c_indoor_humidity",
}


def read_checkins():
    """Return {day: {c_*}} from the Daily Check-in form sheet (matched by Date)."""
    if not (SS_TOKEN and SS_CHECKIN_ID):
        return {}
    try:
        sheet = _request("GET", f"/sheets/{SS_CHECKIN_ID}").json()
    except Exception as e:
        print(f"Check-in read failed (rest of sync OK): {e}")
        return {}
    title = {c["id"]: c["title"] for c in sheet.get("columns", [])}
    out = {}
    for row in sheet.get("rows", []):
        day, vals = None, {}
        for cell in row.get("cells", []):
            t = title.get(cell.get("columnId"))
            v = cell.get("value")
            if t == "Date" and v:
                day = str(v)[:10]
            elif t in CHECKIN_MAP and v not in (None, ""):
                try:
                    vals[CHECKIN_MAP[t]] = float(v)
                except (TypeError, ValueError):
                    pass
        if day and vals:
            out[day] = vals
    return out
