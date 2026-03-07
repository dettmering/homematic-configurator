"""Schedule recommendation engine based on logged thermostat readings."""

from __future__ import annotations

from datetime import datetime


def compute_thermal_model(readings: list[dict]) -> dict | None:
    """Compute thermal characteristics from readings for one device."""
    if len(readings) < 12:
        return None

    heat_rates: list[float] = []
    cool_rates: list[float] = []
    overshoots: list[float] = []

    for i in range(1, len(readings)):
        prev, curr = readings[i - 1], readings[i]

        at_prev = prev.get("actual_temp")
        at_curr = curr.get("actual_temp")
        st_curr = curr.get("set_temp")
        valve = curr.get("valve_state")

        if at_prev is None or at_curr is None:
            continue

        t0 = datetime.fromisoformat(prev["timestamp"])
        t1 = datetime.fromisoformat(curr["timestamp"])
        dt_h = (t1 - t0).total_seconds() / 3600

        if dt_h <= 0 or dt_h > 0.5:
            continue

        delta = at_curr - at_prev
        rate = delta / dt_h

        # Normalize valve: if > 1, assume 0-100 scale
        if valve is not None and valve > 1:
            valve = valve / 100.0

        if valve is not None:
            if valve > 0.1 and st_curr is not None and st_curr > at_curr:
                if rate > 0.05:
                    heat_rates.append(rate)
            elif valve < 0.05:
                if rate < -0.05:
                    cool_rates.append(abs(rate))

        if st_curr is not None and at_curr > st_curr + 0.3:
            overshoots.append(at_curr - st_curr)

    if not heat_rates and not cool_rates:
        return None

    return {
        "heat_up_rate": round(sum(heat_rates) / len(heat_rates), 2) if heat_rates else None,
        "cool_down_rate": round(sum(cool_rates) / len(cool_rates), 2) if cool_rates else None,
        "avg_overshoot": round(sum(overshoots) / len(overshoots), 1) if overshoots else 0.0,
        "samples": len(readings),
        "heating_samples": len(heat_rates),
        "cooling_samples": len(cool_rates),
    }


def _time_to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _min_to_time(mins: int) -> str:
    h, m = divmod(max(0, min(1440, mins)), 60)
    return f"{h:02d}:{m:02d}"


def optimize_schedule(
    schedule: dict[str, list[dict]],
    model: dict,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Generate an optimized schedule and list of changes made."""
    changes: list[dict] = []
    optimized: dict[str, list[dict]] = {}
    heat_rate = model.get("heat_up_rate")
    cool_rate = model.get("cool_down_rate")
    overshoot = model.get("avg_overshoot", 0)

    for day, slots in schedule.items():
        new_slots = [{"end": s["end"], "temp": s["temp"]} for s in slots]

        # --- Overshoot correction ---
        if overshoot > 0.5:
            reduction = min(overshoot * 0.5, 1.5)
            reduction = round(reduction * 2) / 2
            if reduction >= 0.5:
                for slot in new_slots:
                    if slot["temp"] > 18:
                        old_t = slot["temp"]
                        slot["temp"] = old_t - reduction
                        changes.append({
                            "day": day,
                            "type": "temp_reduce",
                            "detail": f"Slot bis {slot['end']}: {old_t}°C -> {slot['temp']}°C "
                                      f"(Ist-Temp ~{overshoot:.1f}°C ueber Soll)",
                        })

        # --- Pre-heat optimization ---
        if heat_rate and heat_rate > 0.3:
            for i in range(1, len(new_slots)):
                prev_temp = new_slots[i - 1]["temp"]
                curr_temp = new_slots[i]["temp"]

                if curr_temp - prev_temp >= 2:
                    delta_t = curr_temp - prev_temp
                    heat_time_min = int((delta_t / heat_rate) * 60)
                    prev_end_min = _time_to_min(new_slots[i - 1]["end"])
                    slot_duration = _time_to_min(new_slots[i]["end"]) - prev_end_min

                    if slot_duration > heat_time_min + 30:
                        savings_min = min(30, slot_duration - heat_time_min - 15)
                        savings_min = (savings_min // 5) * 5  # round to 5 min
                        if savings_min >= 10:
                            new_end = _min_to_time(prev_end_min + savings_min)
                            old_end = new_slots[i - 1]["end"]
                            new_slots[i - 1]["end"] = new_end
                            changes.append({
                                "day": day,
                                "type": "later_start",
                                "detail": f"Heizstart {old_end} -> {new_end} "
                                          f"(Aufheizrate {heat_rate:.1f}°C/h, spart {savings_min} Min)",
                            })

        # --- Early setback ---
        if cool_rate and cool_rate < 0.8:
            for i in range(len(new_slots) - 1):
                curr_temp = new_slots[i]["temp"]
                next_temp = new_slots[i + 1]["temp"]

                if curr_temp - next_temp >= 2:
                    buffer_min = int((1.0 / cool_rate) * 60)
                    savings_min = min(buffer_min, 20)
                    savings_min = (savings_min // 5) * 5
                    if savings_min >= 10:
                        curr_end_min = _time_to_min(new_slots[i]["end"])
                        prev_end_min = _time_to_min(new_slots[i - 1]["end"]) if i > 0 else 0
                        new_end_min = curr_end_min - savings_min
                        if new_end_min > prev_end_min + 5:
                            old_end = new_slots[i]["end"]
                            new_slots[i]["end"] = _min_to_time(new_end_min)
                            changes.append({
                                "day": day,
                                "type": "early_setback",
                                "detail": f"Absenkung {old_end} -> {new_slots[i]['end']} "
                                          f"(Abkuehlrate {cool_rate:.1f}°C/h, Raum bleibt warm)",
                            })

        if new_slots:
            new_slots[-1]["end"] = "24:00"

        optimized[day] = new_slots

    return optimized, changes


def _degree_hours(schedule: dict[str, list[dict]]) -> float:
    total = 0.0
    for slots in schedule.values():
        prev = 0
        for slot in slots:
            end = _time_to_min(slot["end"])
            total += slot["temp"] * (end - prev) / 60
            prev = end
    return total


def estimate_savings(
    original: dict[str, list[dict]],
    optimized: dict[str, list[dict]],
) -> dict:
    orig_dh = _degree_hours(original)
    opt_dh = _degree_hours(optimized)
    savings_dh = orig_dh - opt_dh
    savings_pct = (savings_dh / orig_dh * 100) if orig_dh > 0 else 0

    return {
        "original_degree_hours": round(orig_dh, 1),
        "optimized_degree_hours": round(opt_dh, 1),
        "savings_degree_hours": round(savings_dh, 1),
        "savings_pct": round(savings_pct, 1),
    }


def _deduplicate_changes(changes: list[dict]) -> list[dict]:
    """Group identical change types across days."""
    groups: dict[str, dict] = {}
    for c in changes:
        key = c["type"] + "|" + c["detail"]
        if key not in groups:
            groups[key] = {**c, "_days": [c["day"]]}
        else:
            groups[key]["_days"].append(c["day"])

    result = []
    for item in groups.values():
        days = item.pop("_days")
        if len(days) > 1:
            item["detail"] += f" ({len(days)} Tage)"
        result.append(item)
    return result


def generate_recommendations(
    all_readings: dict[int, list[dict]],
    all_schedules: dict[int, dict],
    device_names: dict[int, str],
) -> dict:
    """Main entry point: generate recommendations for all devices."""
    devices = []
    total_savings_pct = 0.0
    num_with_data = 0

    for peer_id in all_schedules:
        readings = all_readings.get(peer_id, [])
        schedule = all_schedules[peer_id]
        name = device_names.get(peer_id, str(peer_id))

        model = compute_thermal_model(readings)

        if model is None:
            devices.append({
                "peer_id": peer_id,
                "name": name,
                "status": "waiting",
                "message": f"Noch nicht genug Daten ({len(readings)} Messwerte). "
                           f"Mindestens 2-3 Stunden Aufzeichnung noetig.",
                "model": None,
                "changes": [],
                "savings": None,
                "optimized_schedule": None,
            })
            continue

        optimized, changes = optimize_schedule(schedule, model)
        savings = estimate_savings(schedule, optimized)
        unique_changes = _deduplicate_changes(changes)

        num_with_data += 1
        total_savings_pct += savings["savings_pct"]

        status = "optimized" if unique_changes else "optimal"
        message = (
            f"{len(unique_changes)} Optimierungen gefunden, ~{savings['savings_pct']:.1f}% Einsparung"
            if unique_changes
            else "Zeitplan ist bereits gut konfiguriert"
        )

        devices.append({
            "peer_id": peer_id,
            "name": name,
            "status": status,
            "message": message,
            "model": model,
            "changes": unique_changes,
            "savings": savings,
            "optimized_schedule": optimized if unique_changes else None,
        })

    avg_savings = round(total_savings_pct / num_with_data, 1) if num_with_data else 0

    return {
        "devices": devices,
        "avg_savings_pct": avg_savings,
        "num_analyzed": num_with_data,
        "num_waiting": len(all_schedules) - num_with_data,
    }
