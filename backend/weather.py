"""Optional weather forecast integration using OpenWeatherMap free tier."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime


def fetch_forecast(api_key: str, lat: float, lon: float) -> dict | None:
    """Fetch 5-day/3-hour forecast from OpenWeatherMap.

    Returns dict keyed by ISO date string, each with min/max/avg temps
    and hourly breakdown.  Returns None on any error.
    """
    url = (
        f"https://api.openweathermap.org/data/2.5/forecast"
        f"?lat={lat}&lon={lon}&appid={api_key}&units=metric"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "homematic-configurator"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    if data.get("cod") != "200":
        return None

    daily: dict[str, dict] = {}
    for item in data.get("list", []):
        dt = datetime.fromtimestamp(item["dt"])
        date_str = dt.strftime("%Y-%m-%d")
        temp = item["main"]["temp"]

        if date_str not in daily:
            daily[date_str] = {"date": date_str, "temps": [], "weekday": dt.strftime("%A").upper()}
        daily[date_str]["temps"].append({"hour": dt.hour, "temp": round(temp, 1)})

    result = {}
    for date_str, info in daily.items():
        temps = [t["temp"] for t in info["temps"]]
        result[date_str] = {
            "date": date_str,
            "weekday": info["weekday"],
            "min": round(min(temps), 1),
            "max": round(max(temps), 1),
            "avg": round(sum(temps) / len(temps), 1),
            "hourly": info["temps"],
        }

    return result


def get_weekday_outdoor_temps(forecast: dict) -> dict[str, float]:
    """Map forecast days to weekday average outdoor temps.

    Returns e.g. {"MONDAY": 3.2, "TUESDAY": -1.0, ...} for the days
    covered by the forecast.  Days without forecast data are omitted.
    """
    weekday_temps: dict[str, list[float]] = {}
    for info in forecast.values():
        wd = info["weekday"]
        if wd not in weekday_temps:
            weekday_temps[wd] = []
        weekday_temps[wd].append(info["avg"])

    return {wd: round(sum(ts) / len(ts), 1) for wd, ts in weekday_temps.items()}
