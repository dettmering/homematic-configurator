from __future__ import annotations

import sqlite3
import threading
import time
import xmlrpc.client
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.recommend import generate_recommendations
from backend.weather import fetch_forecast, get_weekday_outdoor_temps

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = DATA_DIR / "readings.db"

LOGGING_INTERVAL = 300  # seconds (5 minutes)


def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


# ── SQLite ───────────────────────────────────────────────────────────────────

def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            peer_id INTEGER NOT NULL,
            actual_temp REAL,
            set_temp REAL,
            valve_state REAL,
            outdoor_temp REAL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_readings_peer_time ON readings(peer_id, timestamp)"
    )
    conn.commit()
    conn.close()


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ── Background logger ────────────────────────────────────────────────────────

THERMOSTAT_VALUE_CHANNELS = [1, 2, 3, 4, 5, 6]

# Cache: peer_id -> channel that has ACTUAL_TEMPERATURE
_channel_cache: dict[int, int] = {}


def _find_values_channel(srv, peer_id: int) -> tuple[int | None, dict]:
    """Find which channel has ACTUAL_TEMPERATURE in VALUES."""
    if peer_id in _channel_cache:
        ch = _channel_cache[peer_id]
        try:
            values = srv.getParamset(peer_id, ch, "VALUES")
            if "ACTUAL_TEMPERATURE" in values or "TEMPERATURE" in values:
                return ch, values
        except Exception:
            pass

    for ch in THERMOSTAT_VALUE_CHANNELS:
        try:
            values = srv.getParamset(peer_id, ch, "VALUES")
            if "ACTUAL_TEMPERATURE" in values or "TEMPERATURE" in values:
                _channel_cache[peer_id] = ch
                return ch, values
        except Exception:
            continue
    return None, {}


def _log_once():
    """Poll all thermostats once and store readings."""
    cfg = load_config()
    srv = rpc_connect(cfg["rpc_url"])
    channel = cfg.get("channel", 0)

    # Get thermostat peer IDs
    devices = srv.listDevices()
    thermostat_ids: list[int] = []
    seen: set[int] = set()
    for dev in devices:
        dev_type = dev.get("TYPE", "")
        peer_id = dev.get("ID") if "ID" in dev else None
        parent = dev.get("PARENT", "")
        if parent or peer_id is None or peer_id in seen:
            continue
        has_weekprog = any(t in dev_type for t in THERMOSTAT_TYPES)
        if not has_weekprog:
            try:
                master = srv.getParamset(peer_id, channel, "MASTER")
                has_weekprog = any(k.startswith("ENDTIME_") for k in master)
            except Exception:
                pass
        if has_weekprog:
            seen.add(peer_id)
            thermostat_ids.append(peer_id)

    outdoor_temp = find_outdoor_temperature(srv)
    now = datetime.now(timezone.utc).isoformat()

    with get_db() as conn:
        for pid in thermostat_ids:
            ch, values = _find_values_channel(srv, pid)
            if ch is None:
                continue
            actual = values.get("ACTUAL_TEMPERATURE") or values.get("TEMPERATURE")
            set_temp = values.get("SET_TEMPERATURE") or values.get("SET_POINT_TEMPERATURE")
            valve = values.get("VALVE_STATE")
            if valve is None:
                valve = values.get("LEVEL")

            if actual is None and set_temp is None:
                continue

            conn.execute(
                "INSERT INTO readings (timestamp, peer_id, actual_temp, set_temp, valve_state, outdoor_temp)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    now, pid,
                    float(actual) if actual is not None else None,
                    float(set_temp) if set_temp is not None else None,
                    float(valve) if valve is not None else None,
                    float(outdoor_temp) if outdoor_temp is not None else None,
                ),
            )


def _logging_loop():
    """Background thread: log readings every LOGGING_INTERVAL seconds."""
    while True:
        try:
            _log_once()
        except Exception:
            pass
        time.sleep(LOGGING_INTERVAL)


init_db()

_logger_thread = threading.Thread(target=_logging_loop, daemon=True)
_logger_thread.start()

app = FastAPI()

# ── helpers ──────────────────────────────────────────────────────────────────

DAY_ALIASES = {
    "MONDAY": ["MONDAY", "MONTAG"],
    "TUESDAY": ["TUESDAY", "DIENSTAG"],
    "WEDNESDAY": ["WEDNESDAY", "MITTWOCH"],
    "THURSDAY": ["THURSDAY", "DONNERSTAG"],
    "FRIDAY": ["FRIDAY", "FREITAG"],
    "SATURDAY": ["SATURDAY", "SAMSTAG", "SAT"],
    "SUNDAY": ["SUNDAY", "SONNTAG"],
}
CANONICAL_DAYS = list(DAY_ALIASES.keys())

THERMOSTAT_TYPES = {
    "HM-CC-RT-DN",
    "HM-TC-IT-WM-W-EU",
    "HMIP-eTRV",
    "HMIP-eTRV-2",
    "HMIP-eTRV-B",
    "HMIP-eTRV-C",
    "HMIP-eTRV-E",
    "HmIP-eTRV",
    "HmIP-eTRV-2",
    "HmIP-eTRV-B",
    "HmIP-eTRV-C",
    "HmIP-eTRV-E",
    "HM-CC-VD",
    "HM-CC-TC",
}


def rpc_connect(url: str) -> xmlrpc.client.ServerProxy:
    return xmlrpc.client.ServerProxy(url, allow_none=True)


def detect_day_token(paramset: dict, canonical_day: str) -> str | None:
    for variant in DAY_ALIASES[canonical_day]:
        if f"ENDTIME_{variant}_1" in paramset:
            return variant
    return None


def extract_schedule_from_paramset(paramset: dict, max_slots: int, temp_scale: float) -> dict:
    schedule: dict[str, list] = {}
    for canonical in CANONICAL_DAYS:
        token = detect_day_token(paramset, canonical)
        if not token:
            continue
        slots = []
        for i in range(1, max_slots + 1):
            ek = f"ENDTIME_{token}_{i}"
            tk = f"TEMPERATURE_{token}_{i}"
            if ek not in paramset or tk not in paramset:
                break
            endtime = int(paramset[ek])
            temp_raw = paramset[tk]
            temp_c = float(temp_raw)
            h, m = divmod(endtime, 60)
            slots.append({
                "end": f"{h:02d}:{m:02d}",
                "temp": round(temp_c, 1),
            })
        # filter trailing 24:00 duplicates (unused slots)
        filtered = []
        seen_2400 = False
        for s in slots:
            if s["end"] == "24:00" and seen_2400:
                continue
            filtered.append(s)
            if s["end"] == "24:00":
                seen_2400 = True
        schedule[canonical] = filtered
    return schedule


# ── models ───────────────────────────────────────────────────────────────────

class SlotModel(BaseModel):
    end: str
    temp: float

class DaySchedule(BaseModel):
    day: str
    slots: list[SlotModel]

class ScheduleUpdate(BaseModel):
    peer_id: int
    days: dict[str, list[SlotModel]]


# ── API routes ───────────────────────────────────────────────────────────────

@app.get("/api/config")
def get_config():
    cfg = load_config()
    return {"rpc_url": cfg["rpc_url"], "temp_scale": cfg.get("temp_scale", 2.0),
            "max_slots": cfg.get("max_slots", 13), "channel": cfg.get("channel", 0)}


@app.get("/api/devices")
def list_devices():
    cfg = load_config()
    srv = rpc_connect(cfg["rpc_url"])
    try:
        devices = srv.listDevices()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RPC error: {e}")

    thermostats = []
    seen_ids: set[int] = set()
    channel = cfg.get("channel", 0)
    for dev in devices:
        dev_type = dev.get("TYPE", "")
        peer_id = dev.get("ID") if "ID" in dev else None
        address = dev.get("ADDRESS", "")
        parent = dev.get("PARENT", "")

        if parent:
            continue
        if peer_id is None or peer_id in seen_ids:
            continue

        # Check if device has week program parameters
        has_weekprog = any(t in dev_type for t in THERMOSTAT_TYPES)
        if not has_weekprog:
            try:
                master = srv.getParamset(peer_id, channel, "MASTER")
                has_weekprog = any(k.startswith("ENDTIME_") for k in master)
            except Exception:
                pass

        if has_weekprog:
            seen_ids.add(peer_id)
            thermostats.append({
                "id": peer_id,
                "address": address,
                "type": dev_type,
                "name": dev.get("NAME", address),
            })

    return {"devices": thermostats}


@app.get("/api/devices/{peer_id}/schedule")
def get_schedule(peer_id: int):
    cfg = load_config()
    srv = rpc_connect(cfg["rpc_url"])
    channel = cfg.get("channel", 0)
    max_slots = cfg.get("max_slots", 13)
    temp_scale = cfg.get("temp_scale", 2.0)

    try:
        master = srv.getParamset(peer_id, channel, "MASTER")
    except xmlrpc.client.Fault as e:
        raise HTTPException(status_code=502, detail=f"RPC Fault: {e.faultCode} {e.faultString}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RPC error: {e}")

    schedule = extract_schedule_from_paramset(master, max_slots, temp_scale)
    return {"peer_id": peer_id, "schedule": schedule}


@app.put("/api/devices/{peer_id}/schedule")
def put_schedule(peer_id: int, body: ScheduleUpdate):
    cfg = load_config()
    srv = rpc_connect(cfg["rpc_url"])
    channel = cfg.get("channel", 0)
    max_slots = cfg.get("max_slots", 13)
    temp_scale = cfg.get("temp_scale", 2.0)

    try:
        master = srv.getParamset(peer_id, channel, "MASTER")
    except xmlrpc.client.Fault as e:
        raise HTTPException(status_code=502, detail=f"RPC Fault: {e.faultCode} {e.faultString}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RPC error: {e}")

    updates: dict[str, int] = {}
    for day_key, slots in body.days.items():
        canonical = day_key.upper()
        if canonical not in DAY_ALIASES:
            raise HTTPException(status_code=400, detail=f"Unknown day: {day_key}")

        token = detect_day_token(master, canonical)
        if not token:
            raise HTTPException(status_code=400, detail=f"Device has no week program params for {canonical}")

        # validate slots
        end_minutes_list = []
        for s in slots:
            hh, mm = s.end.strip().split(":")
            em = int(hh) * 60 + int(mm)
            end_minutes_list.append(em)
        if end_minutes_list != sorted(end_minutes_list):
            raise HTTPException(status_code=400, detail=f"Slots for {day_key} must be in ascending time order")
        if not end_minutes_list or end_minutes_list[-1] != 1440:
            raise HTTPException(status_code=400, detail=f"Last slot for {day_key} must end at 24:00")
        if len(slots) > max_slots:
            raise HTTPException(status_code=400, detail=f"Too many slots for {day_key} (max {max_slots})")

        for i, s in enumerate(slots, start=1):
            hh, mm = s.end.strip().split(":")
            em = int(hh) * 60 + int(mm)
            updates[f"ENDTIME_{token}_{i}"] = em
            updates[f"TEMPERATURE_{token}_{i}"] = s.temp

        # clear remaining slots
        for i in range(len(slots) + 1, max_slots + 1):
            ek = f"ENDTIME_{token}_{i}"
            tk = f"TEMPERATURE_{token}_{i}"
            if ek in master:
                updates[ek] = 1440
            if tk in master:
                updates[tk] = slots[-1].temp

    try:
        srv.putParamset(peer_id, channel, "MASTER", updates)
    except xmlrpc.client.Fault as e:
        raise HTTPException(status_code=502, detail=f"RPC Fault: {e.faultCode} {e.faultString}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RPC error: {e}")

    return {"status": "ok", "params_written": len(updates)}


# ── outdoor sensor detection ─────────────────────────────────────────────────

WEATHER_SENSOR_TYPES = {
    "HM-WDS10-TH-O", "HM-WDS40-TH-I", "HM-WDS40-TH-I-2",
    "HmIP-STHO", "HmIP-STHO-A", "HmIP-SWO-PR", "HmIP-SWO-PL",
    "HmIP-SWO-B", "HM-WDS100-C6-O", "HM-WDS100-C6-O-2",
    "HmIP-SPDR", "HM-Sen-Wa-Od",
}


def find_outdoor_temperature(srv: xmlrpc.client.ServerProxy) -> float | None:
    """Try to find an outdoor temperature sensor and return its current reading."""
    try:
        devices = srv.listDevices()
    except Exception:
        return None

    for dev in devices:
        dev_type = dev.get("TYPE", "")
        address = dev.get("ADDRESS", "")
        parent = dev.get("PARENT", "")

        # We want child channels, not parent devices
        if not parent:
            continue

        # Check known weather sensor types
        is_weather = any(t in dev_type for t in WEATHER_SENSOR_TYPES)
        # Also check parent type
        if not is_weather:
            parent_type = ""
            for d in devices:
                if d.get("ADDRESS", "") == parent:
                    parent_type = d.get("TYPE", "")
                    break
            is_weather = any(t in parent_type for t in WEATHER_SENSOR_TYPES)

        if not is_weather:
            continue

        try:
            values = srv.getParamset(address, "VALUES")
            if "ACTUAL_TEMPERATURE" in values:
                return float(values["ACTUAL_TEMPERATURE"])
            if "TEMPERATURE" in values:
                return float(values["TEMPERATURE"])
        except Exception:
            continue

    return None


# ── dashboard metrics ────────────────────────────────────────────────────────

HEATING_THRESHOLD = 17.0  # temperatures above this count as "active heating"


def compute_dashboard_metrics(
    all_schedules: dict[int, dict],
    device_names: dict[int, str],
    outdoor_temp: float | None,
) -> dict:
    """Compute the 5 dashboard metrics from all thermostat schedules."""
    num_devices = len(all_schedules)
    if num_devices == 0:
        return {"error": "Keine Thermostate gefunden"}

    # Per-device daily stats
    device_daily: dict[int, dict[str, dict]] = {}
    for peer_id, sched in all_schedules.items():
        device_daily[peer_id] = {}
        for day, slots in sched.items():
            total_minutes = 0
            weighted_sum = 0.0
            heating_minutes = 0
            temps = []
            prev_end = 0
            for slot in slots:
                h, m = slot["end"].split(":")
                end_min = int(h) * 60 + int(m)
                duration = end_min - prev_end
                if duration <= 0:
                    prev_end = end_min
                    continue
                total_minutes += duration
                weighted_sum += slot["temp"] * duration
                temps.append(slot["temp"])
                if slot["temp"] > HEATING_THRESHOLD:
                    heating_minutes += duration
                prev_end = end_min

            avg_temp = weighted_sum / total_minutes if total_minutes > 0 else 0
            min_temp = min(temps) if temps else 0
            max_temp = max(temps) if temps else 0
            device_daily[peer_id][day] = {
                "avg_temp": round(avg_temp, 1),
                "heating_minutes": heating_minutes,
                "min_temp": min_temp,
                "max_temp": max_temp,
                "spread": round(max_temp - min_temp, 1),
            }

    # 1. Weighted average temperature per day (across all devices)
    daily_avg_temps = {}
    for day in CANONICAL_DAYS:
        temps = [device_daily[pid][day]["avg_temp"]
                 for pid in device_daily if day in device_daily[pid]]
        daily_avg_temps[day] = round(sum(temps) / len(temps), 1) if temps else 0

    overall_avg = round(sum(daily_avg_temps.values()) / 7, 1)

    # 2. Heating hours per day (average across devices)
    daily_heating_hours = {}
    for day in CANONICAL_DAYS:
        mins = [device_daily[pid][day]["heating_minutes"]
                for pid in device_daily if day in device_daily[pid]]
        total = sum(mins)
        daily_heating_hours[day] = round(total / 60, 1)  # total device-hours

    total_heating_hours_week = round(sum(daily_heating_hours.values()), 1)

    # 3. Temperature spread per device
    device_spreads = {}
    for pid in all_schedules:
        spreads = [device_daily[pid][d]["spread"]
                   for d in CANONICAL_DAYS if d in device_daily[pid]]
        device_spreads[pid] = round(max(spreads), 1) if spreads else 0

    avg_spread = round(sum(device_spreads.values()) / num_devices, 1) if num_devices else 0

    # 4. Radiator-degree-hours (Heizkoerper-Grad-Stunden)
    # Sum of (temp * hours) for each device for each day
    daily_degree_hours = {}
    for day in CANONICAL_DAYS:
        dh = 0.0
        for pid in all_schedules:
            sched = all_schedules[pid]
            if day not in sched:
                continue
            prev_end = 0
            for slot in sched[day]:
                h, m = slot["end"].split(":")
                end_min = int(h) * 60 + int(m)
                duration_h = (end_min - prev_end) / 60
                effective_temp = slot["temp"]
                if outdoor_temp is not None:
                    effective_temp = max(0, slot["temp"] - outdoor_temp)
                dh += effective_temp * duration_h
                prev_end = end_min
        daily_degree_hours[day] = round(dh, 1)

    total_degree_hours_week = round(sum(daily_degree_hours.values()), 1)

    # 5. Simultaneity factor - per hour of day, how many devices heat above threshold
    # Returns a 24-element array (one per hour)
    hourly_simultaneity = []
    for hour in range(24):
        mid = hour * 60 + 30  # middle of the hour
        # Average across all 7 days
        total_active = 0
        for day in CANONICAL_DAYS:
            active = 0
            for pid in all_schedules:
                sched = all_schedules[pid]
                if day not in sched:
                    continue
                # Find which slot covers this minute
                prev_end = 0
                for slot in sched[day]:
                    h, m = slot["end"].split(":")
                    end_min = int(h) * 60 + int(m)
                    if prev_end <= mid < end_min:
                        if slot["temp"] > HEATING_THRESHOLD:
                            active += 1
                        break
                    prev_end = end_min
            total_active += active
        hourly_simultaneity.append(round(total_active / 7, 1))

    max_simultaneity = max(hourly_simultaneity) if hourly_simultaneity else 0

    # Heatmap data: for each day & hour, average temp across all devices
    heatmap = {}
    for day in CANONICAL_DAYS:
        hours = []
        for hour in range(24):
            mid = hour * 60 + 30
            temps = []
            for pid in all_schedules:
                sched = all_schedules[pid]
                if day not in sched:
                    continue
                prev_end = 0
                for slot in sched[day]:
                    h, m = slot["end"].split(":")
                    end_min = int(h) * 60 + int(m)
                    if prev_end <= mid < end_min:
                        temps.append(slot["temp"])
                        break
                    prev_end = end_min
            hours.append(round(sum(temps) / len(temps), 1) if temps else 0)
        heatmap[day] = hours

    # Per-device ranking
    device_ranking = []
    for pid in all_schedules:
        dh = 0.0
        for day in CANONICAL_DAYS:
            if day not in all_schedules[pid]:
                continue
            prev_end = 0
            for slot in all_schedules[pid][day]:
                h, m = slot["end"].split(":")
                end_min = int(h) * 60 + int(m)
                duration_h = (end_min - prev_end) / 60
                effective_temp = slot["temp"]
                if outdoor_temp is not None:
                    effective_temp = max(0, slot["temp"] - outdoor_temp)
                dh += effective_temp * duration_h
                prev_end = end_min
        device_ranking.append({
            "peer_id": pid,
            "name": device_names.get(pid, str(pid)),
            "degree_hours_week": round(dh, 1),
            "avg_spread": device_spreads.get(pid, 0),
        })
    device_ranking.sort(key=lambda x: x["degree_hours_week"], reverse=True)

    return {
        "num_devices": num_devices,
        "outdoor_temp": outdoor_temp,
        "avg_temp": overall_avg,
        "daily_avg_temps": daily_avg_temps,
        "total_heating_hours_week": total_heating_hours_week,
        "daily_heating_hours": daily_heating_hours,
        "avg_spread": avg_spread,
        "total_degree_hours_week": total_degree_hours_week,
        "daily_degree_hours": daily_degree_hours,
        "max_simultaneity": max_simultaneity,
        "hourly_simultaneity": hourly_simultaneity,
        "heatmap": heatmap,
        "device_ranking": device_ranking,
    }


@app.get("/api/dashboard")
def get_dashboard():
    cfg = load_config()
    srv = rpc_connect(cfg["rpc_url"])
    channel = cfg.get("channel", 0)
    max_slots = cfg.get("max_slots", 13)
    temp_scale = cfg.get("temp_scale", 2.0)

    # Get all thermostats
    try:
        devices = srv.listDevices()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RPC error: {e}")

    thermostat_peers: list[dict] = []
    seen_ids: set[int] = set()
    for dev in devices:
        dev_type = dev.get("TYPE", "")
        peer_id = dev.get("ID") if "ID" in dev else None
        address = dev.get("ADDRESS", "")
        parent = dev.get("PARENT", "")

        if parent:
            continue
        if peer_id is None or peer_id in seen_ids:
            continue

        has_weekprog = any(t in dev_type for t in THERMOSTAT_TYPES)
        if not has_weekprog:
            try:
                master = srv.getParamset(peer_id, channel, "MASTER")
                has_weekprog = any(k.startswith("ENDTIME_") for k in master)
            except Exception:
                pass

        if has_weekprog:
            seen_ids.add(peer_id)
            thermostat_peers.append({
                "id": peer_id,
                "name": dev.get("NAME", address),
            })

    # Fetch all schedules
    all_schedules: dict[int, dict] = {}
    device_names: dict[int, str] = {}
    for peer in thermostat_peers:
        pid = peer["id"]
        device_names[pid] = peer["name"]
        try:
            master = srv.getParamset(pid, channel, "MASTER")
            all_schedules[pid] = extract_schedule_from_paramset(master, max_slots, temp_scale)
        except Exception:
            continue

    # Try outdoor temperature
    outdoor_temp = find_outdoor_temperature(srv)

    metrics = compute_dashboard_metrics(all_schedules, device_names, outdoor_temp)
    return metrics


@app.get("/api/readings/status")
def readings_status():
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        devices_count = conn.execute("SELECT COUNT(DISTINCT peer_id) FROM readings").fetchone()[0]
        oldest = conn.execute("SELECT MIN(timestamp) FROM readings").fetchone()[0]
        newest = conn.execute("SELECT MAX(timestamp) FROM readings").fetchone()[0]

        per_device = conn.execute(
            "SELECT peer_id, COUNT(*) as cnt, MIN(timestamp) as first, MAX(timestamp) as last "
            "FROM readings GROUP BY peer_id ORDER BY peer_id"
        ).fetchall()

    return {
        "total_readings": total,
        "devices_tracked": devices_count,
        "oldest": oldest,
        "newest": newest,
        "interval_seconds": LOGGING_INTERVAL,
        "per_device": [
            {"peer_id": r[0], "count": r[1], "first": r[2], "last": r[3]}
            for r in per_device
        ],
    }


@app.get("/api/recommendations")
def get_recommendations():
    cfg = load_config()
    srv = rpc_connect(cfg["rpc_url"])
    channel = cfg.get("channel", 0)
    max_slots = cfg.get("max_slots", 13)
    temp_scale = cfg.get("temp_scale", 2.0)

    # Get all thermostat schedules
    try:
        devices = srv.listDevices()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RPC error: {e}")

    thermostat_peers: list[dict] = []
    seen_ids: set[int] = set()
    for dev in devices:
        dev_type = dev.get("TYPE", "")
        peer_id = dev.get("ID") if "ID" in dev else None
        parent = dev.get("PARENT", "")
        if parent or peer_id is None or peer_id in seen_ids:
            continue
        has_weekprog = any(t in dev_type for t in THERMOSTAT_TYPES)
        if not has_weekprog:
            try:
                master = srv.getParamset(peer_id, channel, "MASTER")
                has_weekprog = any(k.startswith("ENDTIME_") for k in master)
            except Exception:
                pass
        if has_weekprog:
            seen_ids.add(peer_id)
            thermostat_peers.append({"id": peer_id, "name": dev.get("NAME", dev.get("ADDRESS", ""))})

    all_schedules: dict[int, dict] = {}
    device_names: dict[int, str] = {}
    for peer in thermostat_peers:
        pid = peer["id"]
        device_names[pid] = peer["name"]
        try:
            master = srv.getParamset(pid, channel, "MASTER")
            all_schedules[pid] = extract_schedule_from_paramset(master, max_slots, temp_scale)
        except Exception:
            continue

    # Get readings from DB (including outdoor_temp for ML features)
    all_readings: dict[int, list[dict]] = {}
    with get_db() as conn:
        for pid in all_schedules:
            rows = conn.execute(
                "SELECT timestamp, actual_temp, set_temp, valve_state, outdoor_temp "
                "FROM readings WHERE peer_id = ? ORDER BY timestamp",
                (pid,),
            ).fetchall()
            all_readings[pid] = [
                {
                    "timestamp": r[0], "actual_temp": r[1], "set_temp": r[2],
                    "valve_state": r[3], "outdoor_temp": r[4],
                }
                for r in rows
            ]

    # Try weather forecast (optional)
    outdoor_temps = None
    forecast_data = None
    weather_cfg = cfg.get("weather")
    if weather_cfg and weather_cfg.get("api_key"):
        forecast_raw = fetch_forecast(
            weather_cfg["api_key"],
            weather_cfg.get("lat", 0),
            weather_cfg.get("lon", 0),
        )
        if forecast_raw:
            outdoor_temps = get_weekday_outdoor_temps(forecast_raw)
            forecast_data = forecast_raw

    return generate_recommendations(
        all_readings, all_schedules, device_names, outdoor_temps, forecast_data,
    )


@app.get("/dashboard")
def dashboard_page():
    return FileResponse(FRONTEND_DIR / "dashboard.html")


# ── serve frontend ───────────────────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
