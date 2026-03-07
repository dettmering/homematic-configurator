#!/usr/bin/env python3
"""
homegear_thermostat_weekprog.py

Setzt ein Wochenprogramm (Wochentage -> Zeitpunkte/Temperaturen) auf ein Homematic BidCoS Heizkörperthermostat
über Homegear per XML-RPC.

Fokus: HM-CC-RT-DN / ähnliche Geräte, bei denen der Wochenplan über MASTER-Paramset in Channel 0
mit Parametern wie ENDTIME_MONDAY_1 und TEMPERATURE_MONDAY_1 gesetzt wird.

Features:
- liest Schedule aus JSON (ohne zusätzliche Dependencies)
- rechnet "HH:MM" -> Minuten seit 00:00 (ENDTIME)
- rechnet °C -> int (standard: temp*2 für 0.5°C Schritte), konfigurierbar
- optional: erkennt automatisch, ob Param-Namen in Englisch (MONDAY) oder Deutsch (MONTAG) vorhanden sind
- dry-run möglich

Beispiel:
  python3 homegear_thermostat_weekprog.py --rpc http://127.0.0.1:2001/ --peer-id 1234 --file schedule.json --apply
"""

from __future__ import annotations

import argparse
import json
import sys
import xmlrpc.client
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional


DAY_ALIASES = {
    # canonical -> variants
    "MONDAY": ["MONDAY", "MONTAG"],
    "TUESDAY": ["TUESDAY", "DIENSTAG"],
    "WEDNESDAY": ["WEDNESDAY", "MITTWOCH"],
    "THURSDAY": ["THURSDAY", "DONNERSTAG"],
    "FRIDAY": ["FRIDAY", "FREITAG"],
    "SATURDAY": ["SATURDAY", "SAMSTAG", "SAT"],
    "SUNDAY": ["SUNDAY", "SONNTAG"],
}

CANONICAL_DAYS = list(DAY_ALIASES.keys())


def hhmm_to_minutes(hhmm: str) -> int:
    try:
        hh, mm = hhmm.strip().split(":")
        h = int(hh)
        m = int(mm)
    except Exception:
        raise ValueError(f"Ungültige Zeit '{hhmm}'. Erwartet HH:MM, z.B. 06:30")
    if not (0 <= h <= 24) or not (0 <= m <= 59):
        raise ValueError(f"Ungültige Zeit '{hhmm}'.")
    if h == 24 and m != 0:
        raise ValueError("24:00 ist erlaubt, aber nicht 24:xx.")
    return h * 60 + m


def temp_to_device_value(temp_c: float, temp_scale: float) -> int:
    # Standard für viele HM-RT: 0.5°C Schritte => scale=2.0 (21.0°C -> 42)
    v = int(round(temp_c * temp_scale))
    return v


def load_schedule_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "days" not in data or not isinstance(data["days"], dict):
        raise ValueError("JSON muss ein Objekt 'days' enthalten.")
    return data


def normalize_day_key(day_key: str) -> str:
    k = day_key.strip().upper()
    # accept short forms
    if k in ("MO",): k = "MONDAY"
    if k in ("DI",): k = "TUESDAY"
    if k in ("MI",): k = "WEDNESDAY"
    if k in ("DO",): k = "THURSDAY"
    if k in ("FR",): k = "FRIDAY"
    if k in ("SA",): k = "SATURDAY"
    if k in ("SO",): k = "SUNDAY"
    # accept German
    for canonical, variants in DAY_ALIASES.items():
        if k in variants:
            return canonical
    raise ValueError(f"Unbekannter Wochentag '{day_key}'.")


@dataclass
class Slot:
    end_hhmm: str
    temp_c: float

    def end_minutes(self) -> int:
        return hhmm_to_minutes(self.end_hhmm)


def validate_slots(slots: List[Slot]) -> None:
    # Sort by end time; enforce increasing and last slot ends at 24:00 (1440)
    endmins = [s.end_minutes() for s in slots]
    if any(endmins[i] <= endmins[i-1] for i in range(1, len(endmins))):
        raise ValueError("Zeiten müssen strikt ansteigend sein (ENDTIME).")
    if endmins[-1] != 1440:
        raise ValueError("Letzter Slot eines Tages muss 24:00 (1440) sein.")


def detect_day_token(paramset_master: Dict[str, object], canonical_day: str) -> Optional[str]:
    """
    Findet, ob Paramset MASTER Keys eher ENDTIME_MONDAY_1 oder ENDTIME_MONTAG_1 usw. nutzt.
    Gibt den gefundenen Day-Token zurück (z.B. 'MONDAY' oder 'MONTAG'), sonst None.
    """
    for variant in DAY_ALIASES[canonical_day]:
        key = f"ENDTIME_{variant}_1"
        if key in paramset_master:
            return variant
    return None


def build_param_updates(
    schedule: dict,
    master_paramset: Dict[str, object],
    temp_scale: float,
    max_slots: int,
) -> Dict[str, int]:
    days_obj = schedule["days"]
    updates: Dict[str, int] = {}

    for day_key, slots_raw in days_obj.items():
        canonical = normalize_day_key(day_key)

        if not isinstance(slots_raw, list) or len(slots_raw) == 0:
            raise ValueError(f"{day_key}: muss eine nicht-leere Liste sein.")

        slots: List[Slot] = []
        for item in slots_raw:
            if not isinstance(item, dict) or "end" not in item or "temp" not in item:
                raise ValueError(f"{day_key}: jeder Slot muss {{'end': 'HH:MM', 'temp': <float>}} enthalten.")
            slots.append(Slot(end_hhmm=str(item["end"]), temp_c=float(item["temp"])))

        # Sortiere nach Zeit und validiere
        slots.sort(key=lambda s: s.end_minutes())
        validate_slots(slots)

        if len(slots) > max_slots:
            raise ValueError(
                f"{day_key}: hat {len(slots)} Slots, aber max_slots={max_slots}. "
                f"Passe max_slots an oder reduziere Slots."
            )

        day_token = detect_day_token(master_paramset, canonical)
        if not day_token:
            raise RuntimeError(
                f"Konnte in MASTER-Paramset keine Keys für {canonical} finden "
                f"(z.B. ENDTIME_MONDAY_1 oder ENDTIME_MONTAG_1). "
                f"Prüfe, ob du Channel 0 und Paramset MASTER hast und ob das Gerät Wochenplan unterstützt."
            )

        # Slots schreiben: ENDTIME_<DAY>_<i>, TEMPERATURE_<DAY>_<i>
        for i, s in enumerate(slots, start=1):
            updates[f"ENDTIME_{day_token}_{i}"] = s.end_minutes()
            updates[f"TEMPERATURE_{day_token}_{i}"] = temp_to_device_value(s.temp_c, temp_scale)

        # optional: restliche Slots "aufräumen", wenn Paramset solche Keys hat
        # (setzt nicht vorhandene Keys nicht)
        for i in range(len(slots) + 1, max_slots + 1):
            ek = f"ENDTIME_{day_token}_{i}"
            tk = f"TEMPERATURE_{day_token}_{i}"
            if ek in master_paramset:
                updates[ek] = 1440
            if tk in master_paramset:
                # setze auf letzte Temperatur, damit es konsistent ist
                updates[tk] = temp_to_device_value(slots[-1].temp_c, temp_scale)

    return updates


def rpc_connect(rpc_url: str) -> xmlrpc.client.ServerProxy:
    # allow_none=True damit Homegear "nil" sauber abbilden kann
    return xmlrpc.client.ServerProxy(rpc_url, allow_none=True)


def rpc_get_master_paramset(srv: xmlrpc.client.ServerProxy, peer_id: int, channel: int) -> Dict[str, object]:
    return srv.getParamset(peer_id, channel, "MASTER")


def rpc_put_master_paramset(srv: xmlrpc.client.ServerProxy, peer_id: int, channel: int, updates: Dict[str, int]) -> None:
    srv.putParamset(peer_id, channel, "MASTER", updates)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rpc", default="http://127.0.0.1:2001/", help="Homegear XML-RPC URL (z.B. http://127.0.0.1:2001/)")
    ap.add_argument("--peer-id", type=int, required=True, help="Homegear PeerId des Thermostats (Device ID)")
    ap.add_argument("--channel", type=int, default=0, help="Channel für Wochenplan (meist 0)")
    ap.add_argument("--file", required=True, help="Pfad zur schedule.json")
    ap.add_argument("--temp-scale", type=float, default=2.0, help="Skalierung für TEMPERATURE-Wert (default 2.0 für 0.5°C)")
    ap.add_argument("--max-slots", type=int, default=13, help="Maximale Slots pro Tag (Geräteabhängig; oft 13)")
    ap.add_argument("--apply", action="store_true", help="Wenn gesetzt, wird wirklich geschrieben. Sonst dry-run.")
    args = ap.parse_args()

    try:
        schedule = load_schedule_json(args.file)
        srv = rpc_connect(args.rpc)

        master = rpc_get_master_paramset(srv, args.peer_id, args.channel)
        updates = build_param_updates(schedule, master, args.temp_scale, args.max_slots)

        # Ausgabe
        print("Geplante Updates (Param -> Wert):")
        for k in sorted(updates.keys()):
            print(f"  {k} = {updates[k]}")

        if not args.apply:
            print("\nDry-run: nichts geschrieben. Nutze --apply zum Anwenden.")
            return 0

        rpc_put_master_paramset(srv, args.peer_id, args.channel, updates)
        print("\nOK: Paramset geschrieben. (Bei Batteriegeräten ggf. erst nach Wakeup wirksam.)")
        return 0

    except xmlrpc.client.Fault as e:
        print(f"RPC Fault: {e.faultCode} {e.faultString}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"Fehler: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

