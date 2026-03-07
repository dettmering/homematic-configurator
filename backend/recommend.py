"""Schedule recommendation engine with ML-based thermal model."""

from __future__ import annotations

from datetime import datetime

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor


# ── ML thermal model ────────────────────────────────────────────────────────

class ThermalModel:
    """ML-based thermal model for a single room.

    Trains gradient-boosted trees to predict heating and cooling rates
    as a function of (outdoor_temp, temp_delta, hour, current_temp).
    Falls back to simple averages when not enough data for ML.
    """

    MIN_ML_SAMPLES = 20

    def __init__(self, readings: list[dict]):
        self.heat_model: GradientBoostingRegressor | None = None
        self.cool_model: GradientBoostingRegressor | None = None
        self.fallback_heat_rate: float | None = None
        self.fallback_cool_rate: float | None = None
        self.avg_overshoot: float = 0.0
        self.num_samples: int = len(readings)
        self.num_heat: int = 0
        self.num_cool: int = 0
        self._train(readings)

    def _train(self, readings: list[dict]) -> None:
        heat_X: list[list[float]] = []
        heat_y: list[float] = []
        cool_X: list[list[float]] = []
        cool_y: list[float] = []
        overshoots: list[float] = []

        for i in range(1, len(readings)):
            prev, curr = readings[i - 1], readings[i]

            at_prev = prev.get("actual_temp")
            at_curr = curr.get("actual_temp")
            st = curr.get("set_temp")
            valve = curr.get("valve_state")
            outdoor = curr.get("outdoor_temp")

            if at_prev is None or at_curr is None:
                continue

            t0 = datetime.fromisoformat(prev["timestamp"])
            t1 = datetime.fromisoformat(curr["timestamp"])
            dt_h = (t1 - t0).total_seconds() / 3600

            if dt_h <= 0 or dt_h > 0.5:
                continue

            rate = (at_curr - at_prev) / dt_h

            # Normalize valve: if > 1, assume 0-100 scale
            if valve is not None and valve > 1:
                valve = valve / 100.0

            hour = t1.hour + t1.minute / 60.0
            delta_t = (st - at_curr) if st is not None else 0.0
            out_t = outdoor if outdoor is not None else 5.0
            features = [out_t, delta_t, hour, at_curr]

            if valve is not None:
                if valve > 0.1 and st is not None and st > at_curr:
                    if rate > 0.05:
                        heat_X.append(features)
                        heat_y.append(rate)
                elif valve < 0.05:
                    if rate < -0.05:
                        cool_X.append(features)
                        cool_y.append(abs(rate))

            if st is not None and at_curr > st + 0.3:
                overshoots.append(at_curr - st)

        # Train ML models if enough data
        if len(heat_y) >= self.MIN_ML_SAMPLES:
            self.heat_model = GradientBoostingRegressor(
                n_estimators=50, max_depth=3, min_samples_leaf=5,
            )
            self.heat_model.fit(np.array(heat_X), np.array(heat_y))

        if len(cool_y) >= self.MIN_ML_SAMPLES:
            self.cool_model = GradientBoostingRegressor(
                n_estimators=50, max_depth=3, min_samples_leaf=5,
            )
            self.cool_model.fit(np.array(cool_X), np.array(cool_y))

        self.fallback_heat_rate = round(sum(heat_y) / len(heat_y), 2) if heat_y else None
        self.fallback_cool_rate = round(sum(cool_y) / len(cool_y), 2) if cool_y else None
        self.avg_overshoot = round(sum(overshoots) / len(overshoots), 1) if overshoots else 0.0
        self.num_heat = len(heat_y)
        self.num_cool = len(cool_y)

    def predict_heat_rate(self, outdoor_temp: float, temp_delta: float,
                          hour: float, current_temp: float) -> float:
        if self.heat_model is not None:
            X = np.array([[outdoor_temp, temp_delta, hour, current_temp]])
            return max(0.1, float(self.heat_model.predict(X)[0]))
        return self.fallback_heat_rate or 1.0

    def predict_cool_rate(self, outdoor_temp: float, temp_delta: float,
                          hour: float, current_temp: float) -> float:
        if self.cool_model is not None:
            X = np.array([[outdoor_temp, temp_delta, hour, current_temp]])
            return max(0.05, float(self.cool_model.predict(X)[0]))
        return self.fallback_cool_rate or 0.5

    @property
    def has_ml(self) -> bool:
        return self.heat_model is not None or self.cool_model is not None

    @property
    def is_valid(self) -> bool:
        return self.fallback_heat_rate is not None or self.fallback_cool_rate is not None

    def to_dict(self) -> dict:
        return {
            "heat_up_rate": self.fallback_heat_rate,
            "cool_down_rate": self.fallback_cool_rate,
            "avg_overshoot": self.avg_overshoot,
            "samples": self.num_samples,
            "heating_samples": self.num_heat,
            "cooling_samples": self.num_cool,
            "ml_active": self.has_ml,
        }


# ── helpers ──────────────────────────────────────────────────────────────────

def _time_to_min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _min_to_time(mins: int) -> str:
    h, m = divmod(max(0, min(1440, mins)), 60)
    return f"{h:02d}:{m:02d}"


# ── schedule optimization ────────────────────────────────────────────────────

def optimize_schedule(
    schedule: dict[str, list[dict]],
    model: ThermalModel,
    outdoor_temps: dict[str, float] | None = None,
) -> tuple[dict[str, list[dict]], list[dict]]:
    """Generate an optimized schedule using the ML thermal model.

    outdoor_temps: optional dict mapping day name -> forecasted outdoor temp.
    When provided, predictions are day-specific.
    """
    changes: list[dict] = []
    optimized: dict[str, list[dict]] = {}
    overshoot = model.avg_overshoot

    for day, slots in schedule.items():
        new_slots = [{"end": s["end"], "temp": s["temp"]} for s in slots]
        out_t = 5.0  # default if no forecast
        if outdoor_temps and day in outdoor_temps:
            out_t = outdoor_temps[day]

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

        # --- Pre-heat optimization (ML-based) ---
        for i in range(1, len(new_slots)):
            prev_temp = new_slots[i - 1]["temp"]
            curr_temp = new_slots[i]["temp"]

            if curr_temp - prev_temp >= 2:
                delta_t = curr_temp - prev_temp
                prev_end_min = _time_to_min(new_slots[i - 1]["end"])
                slot_duration = _time_to_min(new_slots[i]["end"]) - prev_end_min

                # Use ML model to predict heating time at this outdoor temp
                hour = prev_end_min / 60.0
                heat_rate = model.predict_heat_rate(out_t, delta_t, hour, prev_temp)
                heat_time_min = int((delta_t / heat_rate) * 60)

                if slot_duration > heat_time_min + 30:
                    savings_min = min(30, slot_duration - heat_time_min - 15)
                    savings_min = (savings_min // 5) * 5
                    if savings_min >= 10:
                        new_end = _min_to_time(prev_end_min + savings_min)
                        old_end = new_slots[i - 1]["end"]
                        new_slots[i - 1]["end"] = new_end
                        forecast_note = f", Aussen {out_t:.0f}°C" if outdoor_temps else ""
                        changes.append({
                            "day": day,
                            "type": "later_start",
                            "detail": f"Heizstart {old_end} -> {new_end} "
                                      f"(Aufheizrate {heat_rate:.1f}°C/h{forecast_note}, "
                                      f"spart {savings_min} Min)",
                        })

        # --- Early setback (ML-based) ---
        for i in range(len(new_slots) - 1):
            curr_temp = new_slots[i]["temp"]
            next_temp = new_slots[i + 1]["temp"]

            if curr_temp - next_temp >= 2:
                curr_end_min = _time_to_min(new_slots[i]["end"])
                hour = curr_end_min / 60.0
                cool_rate = model.predict_cool_rate(out_t, curr_temp - next_temp, hour, curr_temp)

                if cool_rate < 0.8:
                    buffer_min = int((1.0 / cool_rate) * 60)
                    savings_min = min(buffer_min, 20)
                    savings_min = (savings_min // 5) * 5
                    if savings_min >= 10:
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


def _degree_hours(schedule: dict[str, list[dict]],
                  outdoor_temps: dict[str, float] | None = None) -> float:
    total = 0.0
    for day, slots in schedule.items():
        out_t = outdoor_temps.get(day, 0) if outdoor_temps else 0
        prev = 0
        for slot in slots:
            end = _time_to_min(slot["end"])
            effective = max(0, slot["temp"] - out_t) if outdoor_temps else slot["temp"]
            total += effective * (end - prev) / 60
            prev = end
    return total


def estimate_savings(
    original: dict[str, list[dict]],
    optimized: dict[str, list[dict]],
    outdoor_temps: dict[str, float] | None = None,
) -> dict:
    orig_dh = _degree_hours(original, outdoor_temps)
    opt_dh = _degree_hours(optimized, outdoor_temps)
    savings_dh = orig_dh - opt_dh
    savings_pct = (savings_dh / orig_dh * 100) if orig_dh > 0 else 0

    return {
        "original_degree_hours": round(orig_dh, 1),
        "optimized_degree_hours": round(opt_dh, 1),
        "savings_degree_hours": round(savings_dh, 1),
        "savings_pct": round(savings_pct, 1),
    }


def _deduplicate_changes(changes: list[dict]) -> list[dict]:
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


# ── main entry point ─────────────────────────────────────────────────────────

def generate_recommendations(
    all_readings: dict[int, list[dict]],
    all_schedules: dict[int, dict],
    device_names: dict[int, str],
    outdoor_temps: dict[str, float] | None = None,
    forecast_data: dict | None = None,
) -> dict:
    """Generate recommendations for all devices.

    outdoor_temps: forecasted outdoor temps per weekday (from weather API).
    forecast_data: raw forecast data for display in frontend.
    """
    devices = []
    total_savings_pct = 0.0
    num_with_data = 0
    num_ml = 0

    for peer_id in all_schedules:
        readings = all_readings.get(peer_id, [])
        schedule = all_schedules[peer_id]
        name = device_names.get(peer_id, str(peer_id))

        if len(readings) < 12:
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

        model = ThermalModel(readings)

        if not model.is_valid:
            devices.append({
                "peer_id": peer_id,
                "name": name,
                "status": "waiting",
                "message": f"{len(readings)} Messwerte, aber keine Heiz-/Kuehlphasen erkannt. "
                           f"Mehr Daten noetig.",
                "model": None,
                "changes": [],
                "savings": None,
                "optimized_schedule": None,
            })
            continue

        optimized, changes = optimize_schedule(schedule, model, outdoor_temps)
        savings = estimate_savings(schedule, optimized, outdoor_temps)
        unique_changes = _deduplicate_changes(changes)

        num_with_data += 1
        if model.has_ml:
            num_ml += 1
        total_savings_pct += savings["savings_pct"]

        status = "optimized" if unique_changes else "optimal"
        ml_note = " (ML-Modell)" if model.has_ml else " (Durchschnittswerte)"
        forecast_note = " + Wettervorhersage" if outdoor_temps else ""
        message = (
            f"{len(unique_changes)} Optimierungen, ~{savings['savings_pct']:.1f}% Einsparung"
            f"{ml_note}{forecast_note}"
            if unique_changes
            else f"Zeitplan bereits gut konfiguriert{ml_note}"
        )

        devices.append({
            "peer_id": peer_id,
            "name": name,
            "status": status,
            "message": message,
            "model": model.to_dict(),
            "changes": unique_changes,
            "savings": savings,
            "optimized_schedule": optimized if unique_changes else None,
        })

    avg_savings = round(total_savings_pct / num_with_data, 1) if num_with_data else 0

    result = {
        "devices": devices,
        "avg_savings_pct": avg_savings,
        "num_analyzed": num_with_data,
        "num_waiting": len(all_schedules) - num_with_data,
        "num_ml": num_ml,
        "has_forecast": outdoor_temps is not None,
    }

    if forecast_data:
        result["forecast"] = forecast_data

    return result
