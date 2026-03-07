from __future__ import annotations

import xmlrpc.client
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

def load_config() -> dict:
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)

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
            temp_c = float(temp_raw) / temp_scale
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
            updates[f"TEMPERATURE_{token}_{i}"] = int(round(s.temp * temp_scale))

        # clear remaining slots
        for i in range(len(slots) + 1, max_slots + 1):
            ek = f"ENDTIME_{token}_{i}"
            tk = f"TEMPERATURE_{token}_{i}"
            if ek in master:
                updates[ek] = 1440
            if tk in master:
                updates[tk] = int(round(slots[-1].temp * temp_scale))

    try:
        srv.putParamset(peer_id, channel, "MASTER", updates)
    except xmlrpc.client.Fault as e:
        raise HTTPException(status_code=502, detail=f"RPC Fault: {e.faultCode} {e.faultString}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"RPC error: {e}")

    return {"status": "ok", "params_written": len(updates)}


# ── serve frontend ───────────────────────────────────────────────────────────

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

@app.get("/")
def index():
    return FileResponse(FRONTEND_DIR / "index.html")

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
