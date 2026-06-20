#!/usr/bin/env python3
"""
weather.py
----------
Pulls daily weather for Terry's location (Escaleras / Dominicalito, Costa Rica)
from Open-Meteo (free, no API key) and returns a {day: {...}} dict that merges
straight into the Oura records.

Fields returned per day (keys match the CSV columns and the dashboard metrics):
  w_pressure        mean sea-level barometric pressure (mb / hPa)
  w_pressure_delta  day-over-day change in pressure (mb)
  w_humidity        mean relative humidity (%)
  w_rain_in         total rainfall (inches)
  w_sunshine_h      sunshine duration (hours)
  w_temp_f          mean outdoor temperature (deg F)

Uses the historical archive for the bulk of the range and the forecast endpoint's
"past_days" to fill the most recent days the archive hasn't caught up on yet.

Location can be overridden with env vars WEATHER_LAT / WEATHER_LON / WEATHER_TZ.
"""

import os
import datetime as dt
import requests

LAT = float(os.environ.get("WEATHER_LAT", "9.2236775"))    # Terry's exact location
LON = float(os.environ.get("WEATHER_LON", "-83.8145041"))
TZ = os.environ.get("WEATHER_TZ", "America/Costa_Rica")

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY = "temperature_2m_mean,precipitation_sum,rain_sum,sunshine_duration"
HOURLY = "pressure_msl,relative_humidity_2m"
COMMON = {
    "latitude": LAT, "longitude": LON, "timezone": TZ,
    "daily": DAILY, "hourly": HOURLY,
    "temperature_unit": "fahrenheit", "precipitation_unit": "inch",
}


def _get(url, extra):
    params = dict(COMMON)
    params.update(extra)
    r = requests.get(url, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def _daily(j):
    d = j.get("daily") or {}
    days = d.get("time") or []

    def col(name):
        c = d.get(name)
        return c if c else [None] * len(days)

    tmean, psum, rsum, sun = (col("temperature_2m_mean"), col("precipitation_sum"),
                              col("rain_sum"), col("sunshine_duration"))
    out = {}
    for i, day in enumerate(days):
        rain = rsum[i] if rsum[i] is not None else psum[i]
        out[day] = {
            "w_temp_f": round(tmean[i], 1) if tmean[i] is not None else None,
            "w_rain_in": round(rain, 2) if rain is not None else None,
            "w_sunshine_h": round(sun[i] / 3600.0, 1) if sun[i] is not None else None,
        }
    return out


def _hourly_means(j):
    h = j.get("hourly") or {}
    times = h.get("time") or []
    pres = h.get("pressure_msl") or []
    hum = h.get("relative_humidity_2m") or []
    acc = {}
    for i, t in enumerate(times):
        day = t[:10]
        a = acc.setdefault(day, {"p": [], "h": []})
        if i < len(pres) and pres[i] is not None:
            a["p"].append(pres[i])
        if i < len(hum) and hum[i] is not None:
            a["h"].append(hum[i])
    out = {}
    for day, a in acc.items():
        out[day] = {
            "w_pressure": round(sum(a["p"]) / len(a["p"]), 1) if a["p"] else None,
            "w_humidity": round(sum(a["h"]) / len(a["h"])) if a["h"] else None,
        }
    return out


def _parse(j):
    daily, hourly = _daily(j), _hourly_means(j)
    out = {}
    for day in set(daily) | set(hourly):
        rec = dict(daily.get(day, {}))
        rec.update(hourly.get(day, {}))
        out[day] = rec
    return out


def get_weather(start_date, end_date):
    """Return {day: {w_*}} for the date range (inclusive)."""
    merged = {}
    try:
        merged = _parse(_get(ARCHIVE_URL, {"start_date": start_date, "end_date": end_date}))
    except Exception as e:
        print(f"Weather archive fetch failed: {e}")

    # Fill recent days the archive lags behind on, from the forecast endpoint.
    try:
        recent = _parse(_get(FORECAST_URL, {"past_days": 14, "forecast_days": 1}))
        for day, rec in recent.items():
            if day < start_date or day > end_date:
                continue
            tgt = merged.setdefault(day, {})
            for k, v in rec.items():
                if tgt.get(k) is None and v is not None:
                    tgt[k] = v
    except Exception as e:
        print(f"Weather recent-fill failed (archive still used): {e}")

    # Day-over-day pressure change.
    prev = None
    for day in sorted(merged):
        p = merged[day].get("w_pressure")
        merged[day]["w_pressure_delta"] = (round(p - prev, 1)
                                           if (p is not None and prev is not None) else None)
        if p is not None:
            prev = p
    return merged


if __name__ == "__main__":
    end = dt.date.today()
    start = end - dt.timedelta(days=10)
    wx = get_weather(start.isoformat(), end.isoformat())
    for day in sorted(wx):
        print(day, wx[day])
