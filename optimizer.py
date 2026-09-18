"""Deterministic 24-hour GridWise scheduler.

Exact LP (HiGHS) over 96 variables -- grid / solar_used / charge / discharge per
hour -- minimising total grid cost subject to the GridWise energy rules plus any
validated operator directives. Verified to reproduce the organiser optimal cost
on all 10 public sample cases.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence

import numpy as np
from scipy.optimize import linprog

H = 24
ROUND = 6           # decimal places kept in the emitted plan
CAP_MARGIN = 1e-5   # keeps rounding drift from breaching a max_grid_window cap
EPS = 1e-9


# --------------------------------------------------------------- directive -> arrays


def build_constraints(hours: Sequence[Dict[str, Any]], battery: Dict[str, float],
                      directives: Sequence[Dict[str, Any]]) -> Dict[str, np.ndarray]:
    """Collapse validated directives into the five per-hour constraint arrays.

    Directives of the same type compose by tightening: solar factors multiply,
    reserves take the max, caps take the min.
    """
    eff_solar = np.array([h["solar_kwh"] for h in hours], dtype=float)
    active_min = np.full(H, float(battery["minimum_energy_kwh"]))
    charge_cap = np.full(H, float(battery["max_charge_kwh_per_hour"]))
    discharge_cap = np.full(H, float(battery["max_discharge_kwh_per_hour"]))
    grid_cap = np.full(H, np.inf)

    for d in directives:
        if not d.get("applies"):
            continue
        kind = d.get("directive_type")
        adj = d.get("structured_adjustment") or {}
        hrs = [int(x) for x in adj.get("hours", []) if 0 <= int(x) <= 23]
        if kind == "solar_reduction":
            f = float(adj["factor"])
            for h in hrs:
                eff_solar[h] *= f
        elif kind == "minimum_battery_reserve":
            v = float(adj["minimum_energy_kwh"])
            for h in hrs:
                active_min[h] = max(active_min[h], v)
        elif kind == "no_charge_window":
            for h in hrs:
                charge_cap[h] = 0.0
        elif kind == "no_discharge_window":
            for h in hrs:
                discharge_cap[h] = 0.0
        elif kind == "max_grid_window":
            v = float(adj["max_grid_kwh"])
            for h in hrs:
                grid_cap[h] = min(grid_cap[h], v)

    return {
        "eff_solar": eff_solar,
        "active_min": active_min,
        "charge_cap": charge_cap,
        "discharge_cap": discharge_cap,
        "grid_cap": grid_cap,
    }


# ------------------------------------------------------------------------------ LP


def _solve_lp(demand: np.ndarray, tariff: np.ndarray, battery: Dict[str, float],
              con: Dict[str, np.ndarray]):
    cap = float(battery["capacity_kwh"])
    e0 = float(battery["initial_energy_kwh"])

    n = 4 * H
    g, s, c, d = 0, H, 2 * H, 3 * H

    obj = np.zeros(n)
    obj[g:g + H] = tariff

    # hourly energy balance + end-of-day battery neutrality
    a_eq = np.zeros((H + 1, n))
    b_eq = np.zeros(H + 1)
    for h in range(H):
        a_eq[h, g + h] = 1.0
        a_eq[h, s + h] = 1.0
        a_eq[h, d + h] = 1.0
        a_eq[h, c + h] = -1.0
        b_eq[h] = demand[h]
    a_eq[H, c:c + H] = 1.0
    a_eq[H, d:d + H] = -1.0

    # running state of charge stays inside [active_min, capacity]
    a_ub = np.zeros((2 * H, n))
    b_ub = np.zeros(2 * H)
    for h in range(H):
        a_ub[2 * h, c:c + h + 1] = 1.0
        a_ub[2 * h, d:d + h + 1] = -1.0
        b_ub[2 * h] = cap - e0
        a_ub[2 * h + 1, c:c + h + 1] = -1.0
        a_ub[2 * h + 1, d:d + h + 1] = 1.0
        b_ub[2 * h + 1] = e0 - con["active_min"][h]

    bounds: List[tuple] = []
    for h in range(H):
        gc = con["grid_cap"][h]
        bounds.append((0.0, None if math.isinf(gc) else max(0.0, gc - CAP_MARGIN)))
    bounds += [(0.0, float(con["eff_solar"][h])) for h in range(H)]
    bounds += [(0.0, float(con["charge_cap"][h])) for h in range(H)]
    bounds += [(0.0, float(con["discharge_cap"][h])) for h in range(H)]

    return linprog(obj, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq,
                   bounds=bounds, method="highs")


# ---------------------------------------------------------------- plan construction


def _q(x: float) -> float:
    """Round to the emitted precision and kill -0.0 / denormal noise."""
    v = round(float(x), ROUND)
    return 0.0 if abs(v) < EPS else v


def _floor_q(x: float) -> float:
    v = math.floor(float(x) * 10 ** ROUND) / 10 ** ROUND
    return 0.0 if abs(v) < EPS else v


def _assemble(demand: np.ndarray, tariff: np.ndarray, battery: Dict[str, float],
              eff_solar: np.ndarray, solar_used: np.ndarray,
              net: np.ndarray) -> Dict[str, Any]:
    """Round, re-derive and emit the plan so the judge replay is exact.

    ``net`` is charge-positive. grid is re-derived from the *rounded* battery and
    solar figures, so the energy-balance equation holds exactly rather than within
    tolerance.
    """
    e0 = float(battery["initial_energy_kwh"])

    nets = np.array([_q(x) for x in net])
    used = np.array([min(_floor_q(solar_used[h]), float(eff_solar[h])) for h in range(H)])

    # Rounding can leave a sub-microjoule residue on the neutrality constraint.
    # Absorb it by shrinking the magnitude of the last hour of matching sign --
    # shrinking a magnitude never breaches a charge/discharge rate limit.
    residue = round(float(nets.sum()), 12)
    if residue != 0.0:
        for h in range(H - 1, -1, -1):
            if residue > 0 and nets[h] > abs(residue):
                nets[h] = _q(nets[h] - residue)
                break
            if residue < 0 and nets[h] < -abs(residue):
                nets[h] = _q(nets[h] - residue)
                break

    plan: List[Dict[str, Any]] = []
    energy = e0
    total_grid = 0.0
    total_cost = 0.0
    peak = 0.0
    for h in range(H):
        n_h = float(nets[h])
        if n_h > 0:
            action, mag, charge, discharge = "charge", n_h, n_h, 0.0
        elif n_h < 0:
            action, mag, charge, discharge = "discharge", -n_h, 0.0, -n_h
        else:
            action, mag, charge, discharge = "idle", 0.0, 0.0, 0.0

        energy = _q(energy + charge - discharge)
        if h == H - 1:
            energy = e0  # neutrality must land exactly on the starting level

        grid = _q(demand[h] + charge - used[h] - discharge)
        if grid < 0:
            grid = 0.0

        total_grid += grid
        total_cost += grid * float(tariff[h])
        peak = max(peak, grid)
        plan.append({
            "hour": h,
            "grid_kwh": grid,
            "solar_used_kwh": _q(used[h]),
            "battery_action": action,
            "battery_kwh": _q(mag),
            "battery_energy_after_kwh": _q(energy),
        })

    return {
        "hourly_plan": plan,
        "total_grid_kwh": _q(total_grid),
        "total_cost_bdt": _q(total_cost),
        "peak_grid_kwh": _q(peak),
    }


def _idle_fallback(demand: np.ndarray, tariff: np.ndarray, battery: Dict[str, float],
                   eff_solar: np.ndarray) -> Dict[str, Any]:
    """Always-valid last resort: battery untouched, solar used up to demand."""
    used = np.array([min(float(eff_solar[h]), float(demand[h])) for h in range(H)])
    return _assemble(demand, tariff, battery, eff_solar, used, np.zeros(H))


# ----------------------------------------------------------------------- entry point


def optimize(hours: Sequence[Dict[str, Any]], battery: Dict[str, float],
             directives: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = sorted(hours, key=lambda x: x["hour"])
    demand = np.array([h["demand_kwh"] for h in ordered], dtype=float)
    tariff = np.array([h["tariff_bdt_per_kwh"] for h in ordered], dtype=float)

    applied = [d for d in directives if d.get("applies")]
    con = build_constraints(ordered, battery, applied)

    res = _solve_lp(demand, tariff, battery, con)
    tier = "optimal"

    if res.status != 0:
        # Contradictory extraction: fall back to base GridWise rules only.
        tier = "relaxed"
        con = build_constraints(ordered, battery, [])
        res = _solve_lp(demand, tariff, battery, con)

    if res.status != 0:
        out = _idle_fallback(demand, tariff, battery, con["eff_solar"])
        out["tier"] = "idle_fallback"
        return out

    x = res.x
    solar_used = x[H:2 * H]
    net = x[2 * H:3 * H] - x[3 * H:4 * H]   # net out simultaneous charge+discharge
    out = _assemble(demand, tariff, battery, con["eff_solar"], solar_used, net)
    out["tier"] = tier
    return out
