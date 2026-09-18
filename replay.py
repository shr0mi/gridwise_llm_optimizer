"""Judge-equivalent replay of a finished plan.

Problem Statement 08 lists this as a required guardrail:

    Final replay -- The completed schedule is replayed after optimization to
    verify every extracted directive was actually followed.

Section 11.3 lists exactly what is checked. This module implements that list, so
an invalid plan is caught here and swapped for a safer fallback tier instead of
being sent to the judge. It is deliberately independent of :mod:`optimizer` --
it re-derives everything from the emitted ``hourly_plan`` and the original
request, the same way the judge does.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence

TOL = 0.01          # Problem Statement 11.5: 0.01 kWh / 0.01 BDT
H = 24


def effective_solar(hours: Sequence[Dict[str, Any]],
                    directives: Sequence[Dict[str, Any]]) -> List[float]:
    """Base solar after every applicable solar_reduction directive."""
    ordered = sorted(hours, key=lambda x: x["hour"])
    eff = [float(h["solar_kwh"]) for h in ordered]
    for d in directives:
        if not d.get("applies") or d.get("directive_type") != "solar_reduction":
            continue
        adj = d.get("structured_adjustment") or {}
        factor = float(adj.get("factor", 1.0))
        for h in adj.get("hours", []):
            if 0 <= int(h) < H:
                eff[int(h)] *= factor
    return eff


def directive_limits(battery: Dict[str, float],
                     directives: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Per-hour reserve floors, charge/discharge bans and grid caps."""
    active_min = [float(battery["minimum_energy_kwh"])] * H
    no_charge: set = set()
    no_discharge: set = set()
    grid_cap: Dict[int, float] = {}

    for d in directives:
        if not d.get("applies"):
            continue
        kind = d.get("directive_type")
        adj = d.get("structured_adjustment") or {}
        hrs = [int(h) for h in adj.get("hours", []) if 0 <= int(h) < H]
        if kind == "minimum_battery_reserve":
            value = float(adj.get("minimum_energy_kwh", 0.0))
            for h in hrs:
                active_min[h] = max(active_min[h], value)
        elif kind == "no_charge_window":
            no_charge.update(hrs)
        elif kind == "no_discharge_window":
            no_discharge.update(hrs)
        elif kind == "max_grid_window":
            value = float(adj.get("max_grid_kwh", 0.0))
            for h in hrs:
                grid_cap[h] = min(grid_cap.get(h, float("inf")), value)

    return {"active_min": active_min, "no_charge": no_charge,
            "no_discharge": no_discharge, "grid_cap": grid_cap}


def validate(hours: Sequence[Dict[str, Any]], battery: Dict[str, float],
             directives: Sequence[Dict[str, Any]],
             response: Dict[str, Any]) -> List[str]:
    """Return every rule the plan breaks. An empty list means the plan is valid."""
    errs: List[str] = []
    ordered = sorted(hours, key=lambda x: x["hour"])
    plan = response.get("hourly_plan") or []

    if not isinstance(plan, list) or len(plan) != H:
        return [f"hourly_plan must have {H} entries, got "
                f"{len(plan) if isinstance(plan, list) else type(plan).__name__}"]
    try:
        if sorted(int(p["hour"]) for p in plan) != list(range(H)):
            return ["hourly_plan must contain exactly hours 0..23, each once"]
    except (KeyError, TypeError, ValueError):
        return ["hourly_plan entries must each carry an integer hour"]
    plan = sorted(plan, key=lambda x: int(x["hour"]))

    eff = effective_solar(ordered, directives)
    lim = directive_limits(battery, directives)
    capacity = float(battery["capacity_kwh"])
    e0 = float(battery["initial_energy_kwh"])
    max_charge = float(battery["max_charge_kwh_per_hour"])
    max_discharge = float(battery["max_discharge_kwh_per_hour"])

    energy = e0
    total_grid = total_cost = peak = 0.0

    for h, p in enumerate(plan):
        try:
            grid = float(p["grid_kwh"])
            solar = float(p["solar_used_kwh"])
            action = p["battery_action"]
            magnitude = float(p["battery_kwh"])
            after = float(p["battery_energy_after_kwh"])
        except (KeyError, TypeError, ValueError):
            errs.append(f"h{h}: hourly_plan entry is missing or malformed")
            continue

        for name, value in (("grid_kwh", grid), ("solar_used_kwh", solar),
                            ("battery_kwh", magnitude),
                            ("battery_energy_after_kwh", after)):
            if value != value or value in (float("inf"), float("-inf")):
                errs.append(f"h{h}: {name} is not finite")
        if grid < -TOL or solar < -TOL or magnitude < -TOL:
            errs.append(f"h{h}: negative energy value")

        if action not in ("charge", "discharge", "idle"):
            errs.append(f"h{h}: battery_action {action!r} is not charge/discharge/idle")
            continue
        if action == "idle" and abs(magnitude) > TOL:
            errs.append(f"h{h}: idle hour must have battery_kwh = 0, got {magnitude}")

        charge = magnitude if action == "charge" else 0.0
        discharge = magnitude if action == "discharge" else 0.0

        if solar > eff[h] + TOL:
            errs.append(f"h{h}: solar_used {solar} exceeds effective solar {eff[h]:.4f}")
        if charge > max_charge + TOL:
            errs.append(f"h{h}: charge {charge} exceeds max_charge_kwh_per_hour")
        if discharge > max_discharge + TOL:
            errs.append(f"h{h}: discharge {discharge} exceeds max_discharge_kwh_per_hour")
        if h in lim["no_charge"] and charge > TOL:
            errs.append(f"h{h}: charged inside a no_charge_window")
        if h in lim["no_discharge"] and discharge > TOL:
            errs.append(f"h{h}: discharged inside a no_discharge_window")
        if h in lim["grid_cap"] and grid > lim["grid_cap"][h] + TOL:
            errs.append(f"h{h}: grid {grid} exceeds max_grid_kwh {lim['grid_cap'][h]}")

        demand = float(ordered[h]["demand_kwh"])
        if abs((grid + solar + discharge) - (demand + charge)) > TOL:
            errs.append(f"h{h}: energy balance violated")

        expected = energy + charge - discharge
        if abs(expected - after) > TOL:
            errs.append(f"h{h}: battery_energy_after_kwh {after} != {expected:.4f}")
        energy = after

        if energy < lim["active_min"][h] - TOL:
            errs.append(f"h{h}: battery {energy} below required minimum "
                        f"{lim['active_min'][h]}")
        if energy > capacity + TOL:
            errs.append(f"h{h}: battery {energy} above capacity {capacity}")

        total_grid += grid
        total_cost += grid * float(ordered[h]["tariff_bdt_per_kwh"])
        peak = max(peak, grid)

    if abs(energy - e0) > TOL:
        errs.append(f"end-of-day battery {energy} != initial {e0}")

    for name, recomputed in (("total_grid_kwh", total_grid),
                             ("total_cost_bdt", total_cost),
                             ("peak_grid_kwh", peak)):
        try:
            reported = float(response.get(name))
        except (TypeError, ValueError):
            errs.append(f"{name} is missing or not a number")
            continue
        if abs(reported - recomputed) > TOL:
            errs.append(f"{name} {reported} does not match hourly_plan "
                        f"({recomputed:.4f})")

    return errs
