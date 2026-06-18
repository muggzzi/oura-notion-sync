#!/usr/bin/env python3
"""
oura_to_notion.py
-----------------
Pulls Oura Ring data from the Oura API (v2) and writes one row per night into a
Notion database. Runs on a rolling lookback window and UPSERTS (updates an
existing row for a date, or creates it if missing), so if you forget to sync
your ring for a day or two, the gap quietly fills in on the next run.

Designed to run unattended on a schedule (see .github/workflows/oura-sync.yml),
but you can also run it by hand:  python oura_to_notion.py

Environment variables (set these as secrets, never hard-code them):
  OURA_TOKEN     - Oura Personal Access Token  (cloud.ouraring.com/personal-access-tokens)
  NOTION_TOKEN   - Notion internal integration token (notion.so/my-integrations)
  NOTION_DB_ID   - The ID of your Notion "Oura" database
  LOOKBACK_DAYS  - optional, how many days back to refresh each run (default 7)

Notes on choices made here:
  * Temperature is stored as a deviation in Fahrenheit (Oura reports it in Celsius;
    a delta of X C = X * 1.8 F).  This matches your preference for Fahrenheit.
  * "Resting HR" uses Oura's lowest_heart_rate (what we've been calling your low HR).
  * Bedtime/Wake are stored as local clock times (HH:MM) from bedtime_start/end --
    the real clock bedtime the flattened export couldn't give us.
  * Only the main overnight sleep (type "long_sleep", or the longest period that
    day) is recorded, so naps don't overwrite the night.
"""

import os
import sys
import datetime as dt
import requests

# ----------------------------------------------------------------------------- 
# Config
# -----------------------------------------------------------------------------
OURA_TOKEN = os.environ.get("OURA_TOKEN", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()
NOTION_DB_ID = os.environ.get("NOTION_DB_ID", "").strip()
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "7"))

OURA_BASE = "https://api.ouraring.com/v2/usercollection"
NOTION_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

if not (OURA_TOKEN and NOTION_TOKEN and NOTION_DB_ID):
    sys.exit("Missing one of OURA_TOKEN / NOTION_TOKEN / NOTION_DB_ID.")

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
    # temperature DEVIATION conversion: delta degrees C -> delta degrees F
    return round(c * 1.8, 2) if isinstance(c, (int, float)) else None

def local_hhmm(iso_ts):
    if not iso_ts:
        return None
    try:
        return dt.datetime.fromisoformat(iso_ts).strftime("%H:%M")
    except ValueError:
        return None

# -----------------------------------------------------------------------------
# Oura fetching (handles pagination via next_token)
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
    """Choose the main overnight sleep, ignoring naps."""
    longs = [p for p in periods_for_day if p.get("type") == "long_sleep"]
    pool = longs if longs else periods_for_day
    if not pool:
        return None
    return max(pool, key=lambda p: p.get("total_sleep_duration") or 0)

def fetch_range(start_date, end_date):
    """Return {day: {merged metrics}} for the date range."""
    # Detailed /sleep filters by bedtime_start, so widen the window one day back
    # and re-key by the returned 'day' field to avoid missing overnight sleeps.
    sleep_start = (dt.date.fromisoformat(start_date) - dt.timedelta(days=1)).isoformat()

    detailed = oura_get("sleep", sleep_start, end_date)
    daily_sleep = oura_get("daily_sleep", start_date, end_date)
    readiness = oura_get("daily_readiness", start_date, end_date)
    spo2 = oura_get("daily_spo2", start_date, end_date)
    stress = oura_get("daily_stress", start_date, end_date)
    try:
        tags = oura_get("enhanced_tag", start_date, end_date)
    except requests.HTTPError:
        tags = []  # tags are optional; never let them break the run

    # group detailed sleep periods by day, then pick the main one
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

    # collect tag comments per day
    notes_by_day = {}
    for t in tags:
        day = t.get("start_day") or t.get("day")
        text = t.get("comment") or t.get("tag_type_code") or ""
        if day and text:
            notes_by_day.setdefault(day, []).append(text)
    for day, items in notes_by_day.items():
        rec(day)["notes"] = "; ".join(items)[:1900]

    # keep only days inside the requested window
    return {d: v for d, v in records.items() if start_date <= d <= end_date}

# -----------------------------------------------------------------------------
# Notion writing (find -> update or create)
# -----------------------------------------------------------------------------
def notion_find_page(day):
    url = f"{NOTION_BASE}/databases/{NOTION_DB_ID}/query"
    payload = {"filter": {"property": "Date", "date": {"equals": day}}, "page_size": 1}
    r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
    r.raise_for_status()
    results = r.json().get("results", [])
    return results[0]["id"] if results else None

def num(v):
    return {"number": v} if isinstance(v, (int, float)) else None

def txt(v):
    return {"rich_text": [{"text": {"content": str(v)}}]} if v not in (None, "") else None

def sel(v):
    return {"select": {"name": str(v)}} if v not in (None, "") else None

def build_properties(r):
    """Map a merged record to Notion properties. None values are dropped so we
    never overwrite good data with blanks on a partial pull."""
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
        url = f"{NOTION_BASE}/pages/{page_id}"
        r = requests.patch(url, headers=NOTION_HEADERS, json={"properties": props}, timeout=30)
        action = "updated"
    else:
        url = f"{NOTION_BASE}/pages"
        payload = {"parent": {"database_id": NOTION_DB_ID}, "properties": props}
        r = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
        action = "created"
    r.raise_for_status()
    return action

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
def main():
    end = dt.date.today()
    start = end - dt.timedelta(days=LOOKBACK_DAYS)
    start_s, end_s = start.isoformat(), end.isoformat()
    print(f"Syncing Oura -> Notion for {start_s} .. {end_s}")

    records = fetch_range(start_s, end_s)
    if not records:
        print("No Oura data in window yet (have you opened the Oura app to sync?).")
        return

    created = updated = 0
    for day in sorted(records):
        try:
            action = upsert(records[day])
            counts = {"created": 1, "updated": 1}
            if action == "created":
                created += 1
            else:
                updated += 1
            print(f"  {day}: {action}")
        except requests.HTTPError as e:
            print(f"  {day}: ERROR {e} -> {getattr(e.response,'text','')[:300]}")

    print(f"Done. {created} created, {updated} updated.")

if __name__ == "__main__":
    main()
