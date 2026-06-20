#!/usr/bin/env python3
"""
oura_to_notion.py
-----------------
Pulls Oura Ring data from the Oura API (v2) and:
  1. Writes one row per night into a Notion database (rolling lookback + upsert).
  2. Exports your full history as a single CSV to Dropbox, so it can be read
     and analyzed on demand without any manual uploads.

Runs unattended on a schedule (see .github/workflows/oura-sync.yml); can also be
run by hand.

Environment variables (set as secrets):
  OURA_TOKEN              Oura Personal Access Token
  NOTION_TOKEN           Notion internal integration token
  NOTION_DB_ID           ID of the Notion "Oura" database
  LOOKBACK_DAYS          days back to refresh in Notion each run (default 7)
  CSV_DAYS               days of history to write to the Dropbox CSV (default 400)
  DROPBOX_APP_KEY        Dropbox app key
  DROPBOX_APP_SECRET     Dropbox app secret
  DROPBOX_REFRESH_TOKEN  Dropbox OAuth refresh token (for unattended uploads)
  DROPBOX_CSV_PATH       path in Dropbox for the CSV (default /oura_history.csv)
  DROPBOX_AUTH_CODE      one-time use: exchange an auth code for a refresh token

One-time Dropbox setup helper:
  Run the workflow with the "dropbox_auth_code" input set to the authorization
  code from Dropbox. The script prints a refresh token to save as a secret.
"""

import os
import sys
import time
import json
import datetime as dt
import io
import csv
import requests

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
OURA_TOKEN = os.environ.get("OURA_TOKEN", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "").strip()
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))
CSV_DAYS = int(os.environ.get("CSV_DAYS", "400"))

DBX_KEY = os.environ.get("DROPBOX_APP_KEY", "").strip()
DBX_SECRET = os.environ.get("DROPBOX_APP_SECRET", "").strip()
DBX_REFRESH = os.environ.get("DROPBOX_REFRESH_TOKEN", "").strip()
DBX_CSV_PATH = os.environ.get("DROPBOX_CSV_PATH", "/oura_history.csv").strip()

OURA_BASE = "https://api.ouraring.com/v2/usercollection"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

OURA_HEADERS = {"Authorization": f"Bearer {OURA_TOKEN}"}
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------
def secs_to_min(s):
    return round(s / 60.0, 1) if isinstance(s, (int, float)) else None

def secs_to_hours(s):
    return round(s / 3600.0, 2) if isinstance(s, (int, float)) else None

def c_dev_to_f(c):
    return round(c * 1.8, 2) if isinstance(c, (int, float)) else None

def local_hhmm(iso_ts):
    if not iso_ts:
        return None
    try:
        return dt.datetime.fromisoformat(iso_ts).strftime("%H:%M")
    except ValueError:
        return None

# -----------------------------------------------------------------------------
# Oura fetching
# -----------------------------------------------------------------------------
def oura_get(path, start_date, end_date):
    out = []
    params = {"start_date": start_date, "end_date": end_date}
    url = f"{OURA_BASE}/{path}"
    while True:
        r = requests.get(url, headers=OURA_HEADERS, params=params, timeout=30)
        r.raise_for_status()
        body = r.json()
        out.extend(body.get("data", []))
        token = body.get("next_token")
        if not token:
            break
        params["next_token"] = token
    return out

def pick_main_sleep(periods_for_day):
    longs = [p for p in periods_for_day if p.get("type") == "long_sleep"]
    pool = longs if longs else periods_for_day
    if not pool:
        return None
    return max(pool, key=lambda p: p.get("total_sleep_duration") or 0)

def fetch_range(start_date, end_date):
    """Return {day: {merged metrics}} for the date range."""
    sleep_start = (dt.date.fromisoformat(start_date) - dt.timedelta(days=1)).isoformat()

    detailed = oura_get("sleep", sleep_start, end_date)
    daily_sleep = oura_get("daily_sleep", start_date, end_date)
    readiness = oura_get("daily_readiness", start_date, end_date)
    spo2 = oura_get("daily_spo2", start_date, end_date)
    stress = oura_get("daily_stress", start_date, end_date)
    try:
        tags = oura_get("enhanced_tag", start_date, end_date)
    except requests.HTTPError:
        tags = []

    by_day_periods = {}
    for p in detailed:
        by_day_periods.setdefault(p.get("day"), []).append(p)

    records = {}

    def rec(day):
        return records.setdefault(day, {"day": day})

    for day, periods in by_day_periods.items():
        main = pick_main_sleep(periods)
        if not main:
            continue
        r = rec(day)
        r["total_sleep_h"] = secs_to_hours(main.get("total_sleep_duration"))
        r["time_in_bed_h"] = secs_to_hours(main.get("time_in_bed"))
        r["efficiency"] = main.get("efficiency")
        r["deep_min"] = secs_to_min(main.get("deep_sleep_duration"))
        r["rem_min"] = secs_to_min(main.get("rem_sleep_duration"))
        r["light_min"] = secs_to_min(main.get("light_sleep_duration"))
        r["latency_min"] = secs_to_min(main.get("latency"))
        r["avg_hrv"] = main.get("average_hrv")
        r["resting_hr"] = main.get("lowest_heart_rate")
        r["avg_hr"] = round(main["average_heart_rate"], 1) if main.get("average_heart_rate") else None
        r["resp_rate"] = round(main["average_breath"], 1) if main.get("average_breath") else None
        r["bedtime"] = local_hhmm(main.get("bedtime_start"))
        r["wake"] = local_hhmm(main.get("bedtime_end"))

    for d in daily_sleep:
        rec(d.get("day"))["sleep_score"] = d.get("score")

    for d in readiness:
        r = rec(d.get("day"))
        r["readiness_score"] = d.get("score")
        r["temp_dev_f"] = c_dev_to_f(d.get("temperature_deviation"))
        contrib = d.get("contributors") or {}
        r["hrv_balance"] = contrib.get("hrv_balance")

    for d in spo2:
        pct = (d.get("spo2_percentage") or {}).get("average")
        if pct is not None:
            rec(d.get("day"))["spo2"] = round(pct, 1)

    for d in stress:
        r = rec(d.get("day"))
        r["stress_high_min"] = secs_to_min(d.get("stress_high"))
        r["recovery_high_min"] = secs_to_min(d.get("recovery_high"))
        r["stress_summary"] = d.get("day_summary")

    notes_by_day = {}
    for t in tags:
        day = t.get("start_day") or t.get("day")
        text = t.get("comment") or t.get("tag_type_code") or ""
        if day and text:
            notes_by_day.setdefault(day, []).append(text)
    for day, items in notes_by_day.items():
        rec(day)["notes"] = "; ".join(items)[:1900]

    return {d: v for d, v in records.items() if start_date <= d <= end_date}

# -----------------------------------------------------------------------------
# Notion writing
# -----------------------------------------------------------------------------
def notion_request(method, url, payload=None, max_retries=6):
    for attempt in range(max_retries):
        r = requests.request(method, url, headers=NOTION_HEADERS, json=payload, timeout=30)
        if r.status_code == 429:
            wait = float(r.headers.get("Retry-After", 1)) + 0.5
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(1.5 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r

def notion_find_page(day):
    url = f"{NOTION_BASE}/databases/{NOTION_DB_ID}/query"
    payload = {"filter": {"property": "Date", "date": {"equals": day}}, "page_size": 1}
    r = notion_request("POST", url, payload)
    results = r.json().get("results", [])
    return results[0]["id"] if results else None

def num(v):
    return {"number": v} if isinstance(v, (int, float)) else None

def txt(v):
    return {"rich_text": [{"text": {"content": str(v)}}]} if v not in (None, "") else None

def sel(v):
    return {"select": {"name": str(v)}} if v not in (None, "") else None

def build_properties(r):
    day = r["day"]
    props = {
        "Name": {"title": [{"text": {"content": day}}]},
        "Date": {"date": {"start": day}},
        "Sleep Score": num(r.get("sleep_score")),
        "Readiness Score": num(r.get("readiness_score")),
        "Total Sleep (h)": num(r.get("total_sleep_h")),
        "Time in Bed (h)": num(r.get("time_in_bed_h")),
        "Efficiency (%)": num(r.get("efficiency")),
        "Deep Sleep (min)": num(r.get("deep_min")),
        "REM Sleep (min)": num(r.get("rem_min")),
        "Light Sleep (min)": num(r.get("light_min")),
        "Sleep Latency (min)": num(r.get("latency_min")),
        "Avg HRV (ms)": num(r.get("avg_hrv")),
        "HRV Balance": num(r.get("hrv_balance")),
        "Resting HR (bpm)": num(r.get("resting_hr")),
        "Avg HR (bpm)": num(r.get("avg_hr")),
        "Respiratory Rate": num(r.get("resp_rate")),
        "Temp Deviation (F)": num(r.get("temp_dev_f")),
        "SpO2 (%)": num(r.get("spo2")),
        "Bedtime": txt(r.get("bedtime")),
        "Wake": txt(r.get("wake")),
        "Daytime Stress High (min)": num(r.get("stress_high_min")),
        "Daytime Recovery (min)": num(r.get("recovery_high_min")),
        "Stress Summary": sel(r.get("stress_summary")),
        "Notes": txt(r.get("notes")),
        "Synced At": txt(dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")),
    }
    return {k: v for k, v in props.items() if v is not None}

def upsert(record):
    props = build_properties(record)
    page_id = notion_find_page(record["day"])
    if page_id:
        notion_request("PATCH", f"{NOTION_BASE}/pages/{page_id}", {"properties": props})
        return "updated"
    payload = {"parent": {"database_id": NOTION_DB_ID}, "properties": props}
    notion_request("POST", f"{NOTION_BASE}/pages", payload)
    return "created"

# -----------------------------------------------------------------------------
# Dropbox CSV export
# -----------------------------------------------------------------------------
CSV_COLUMNS = [
    "date", "total_sleep_h", "time_in_bed_h", "efficiency", "deep_min", "rem_min",
    "light_min", "latency_min", "avg_hrv", "hrv_balance", "resting_hr", "avg_hr",
    "resp_rate", "temp_dev_f", "spo2", "sleep_score", "readiness_score",
    "bedtime", "wake", "stress_high_min", "recovery_high_min", "stress_summary", "notes",
]

def records_to_csv(records):
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=CSV_COLUMNS)
    w.writeheader()
    for day in sorted(records):
        r = records[day]
        row = {c: r.get(c) for c in CSV_COLUMNS}
        row["date"] = day
        w.writerow(row)
    return out.getvalue()

def dropbox_access_token():
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={"grant_type": "refresh_token", "refresh_token": DBX_REFRESH},
        auth=(DBX_KEY, DBX_SECRET), timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]

def dropbox_upload(token, path, content_bytes):
    r = requests.post(
        "https://content.dropboxapi.com/2/files/upload",
        headers={
            "Authorization": f"Bearer {token}",
            "Dropbox-API-Arg": json.dumps({"path": path, "mode": "overwrite", "mute": True}),
            "Content-Type": "application/octet-stream",
        },
        data=content_bytes, timeout=60,
    )
    r.raise_for_status()

def export_csv_to_dropbox(records):
    if not (DBX_KEY and DBX_SECRET and DBX_REFRESH):
        print("Dropbox not configured; skipping CSV export.")
        return
    try:
        token = dropbox_access_token()
        csv_text = records_to_csv(records)
        dropbox_upload(token, DBX_CSV_PATH, csv_text.encode("utf-8"))
        print(f"CSV exported to Dropbox at {DBX_CSV_PATH} ({len(records)} nights).")
    except Exception as e:
        print(f"Dropbox export failed (sync still OK): {e}")

def dropbox_auth_exchange(code):
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={"code": code, "grant_type": "authorization_code"},
        auth=(DBX_KEY, DBX_SECRET), timeout=30,
    )
    r.raise_for_status()
    return r.json()

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    # One-time helper: exchange an auth code for a refresh token, then exit.
    code = os.environ.get("DROPBOX_AUTH_CODE", "").strip()
    if code:
        if not (DBX_KEY and DBX_SECRET):
            sys.exit("Set DROPBOX_APP_KEY and DROPBOX_APP_SECRET secrets first.")
        data = dropbox_auth_exchange(code)
        rt = data.get("refresh_token", "(no refresh_token returned)")
        print("==== SAVE THIS VALUE AS THE DROPBOX_REFRESH_TOKEN SECRET ====")
        print(rt)
        print("==== then delete this workflow run from the Actions tab ====")
        return

    if not (OURA_TOKEN and NOTION_TOKEN and NOTION_DB_ID):
        sys.exit("Missing one of OURA_TOKEN / NOTION_TOKEN / NOTION_DB_ID.")

    end = dt.date.today()
    lookback_start = end - dt.timedelta(days=LOOKBACK_DAYS)
    csv_start = end - dt.timedelta(days=CSV_DAYS)
    fetch_start = min(lookback_start, csv_start)
    print(f"Fetching Oura data {fetch_start} .. {end}")

    records = fetch_range(fetch_start.isoformat(), end.isoformat())
    if not records:
        print("No Oura data in window yet (open the Oura app to sync the ring?).")
        return

    # Notion: upsert only the recent lookback window (keeps daily writes light).
    lb = lookback_start.isoformat()
    to_upsert = {d: v for d, v in records.items() if d >= lb}
    created = updated = 0
    for day in sorted(to_upsert):
        try:
            action = upsert(to_upsert[day])
            if action == "created":
                created += 1
            else:
                updated += 1
            print(f"  Notion {day}: {action}")
            time.sleep(0.34)
        except requests.HTTPError as e:
            print(f"  Notion {day}: ERROR {e} -> {getattr(e.response,'text','')[:300]}")
    print(f"Notion done. {created} created, {updated} updated.")

    # Dropbox: write the full history CSV for on-demand analysis.
    export_csv_to_dropbox(records)

    # Smartsheet: push Oura (and later weather) into the live Daily Metrics sheet.
    try:
        import smartsheet_sync
        smartsheet_sync.sync(records)
    except Exception as e:
        print(f"Smartsheet sync failed (rest of sync OK): {e}")

if __name__ == "__main__":
    main()
