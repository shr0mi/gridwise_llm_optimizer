"""Judge-equivalent harness for the 10 public sample cases.

Modes:
  python test_local.py --offline            # optimizer only, ground-truth directives
  python test_local.py --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Tuple

TOL = 0.01
HERE = os.path.dirname(os.path.abspath(__file__))


# ------------------------------------------------------------------- replay checks


def effective_solar(hours: List[Dict[str, Any]], directives: List[Dict[str, Any]]) -> List[float]:
    eff = [float(h["solar_kwh"]) for h in sorted(hours, key=lambda x: x["hour"])]
    for d in directives:
        if d.get("applies") and d.get("directive_type") == "solar_reduction":
            adj = d["structured_adjustment"]
            for h in adj["hours"]:
                eff[h] *= float(adj["factor"])
    return eff


def replay(case_input: Dict[str, Any], resp: Dict[str, Any],
           directives: List[Dict[str, Any]]) -> List[str]:
    """Replay the returned plan against `directives` the way the judge would."""
    errs: List[str] = []
    hours = sorted(case_input["hours"], key=lambda x: x["hour"])
    bat = case_input["battery"]
    plan = resp.get("hourly_plan") or []

    if sorted(p.get("hour") for p in plan) != list(range(24)):
        return ["hourly_plan must contain exactly hours 0..23"]
    plan = sorted(plan, key=lambda x: x["hour"])

    eff = effective_solar(hours, directives)
    active_min = [float(bat["minimum_energy_kwh"])] * 24
    no_charge, no_discharge = set(), set()
    grid_cap: Dict[int, float] = {}
    for d in directives:
        if not d.get("applies"):
            continue
        t, adj = d["directive_type"], d.get("structured_adjustment") or {}
        if t == "minimum_battery_reserve":
            for h in adj["hours"]:
                active_min[h] = max(active_min[h], float(adj["minimum_energy_kwh"]))
        elif t == "no_charge_window":
            no_charge.update(adj["hours"])
        elif t == "no_discharge_window":
            no_discharge.update(adj["hours"])
        elif t == "max_grid_window":
            for h in adj["hours"]:
                grid_cap[h] = min(grid_cap.get(h, float("inf")), float(adj["max_grid_kwh"]))

    energy = float(bat["initial_energy_kwh"])
    total_grid = total_cost = peak = 0.0
    for h, p in enumerate(plan):
        g, s = float(p["grid_kwh"]), float(p["solar_used_kwh"])
        act, mag = p["battery_action"], float(p["battery_kwh"])
        if g < -TOL or s < -TOL or mag < -TOL:
            errs.append(f"h{h}: negative value")
        if act not in ("charge", "discharge", "idle"):
            errs.append(f"h{h}: bad battery_action {act!r}")
        if act == "idle" and abs(mag) > TOL:
            errs.append(f"h{h}: idle with battery_kwh={mag}")
        if s > eff[h] + TOL:
            errs.append(f"h{h}: solar_used {s} > effective solar {eff[h]:.4f}")

        charge = mag if act == "charge" else 0.0
        discharge = mag if act == "discharge" else 0.0
        if charge > float(bat["max_charge_kwh_per_hour"]) + TOL:
            errs.append(f"h{h}: charge rate exceeded")
        if discharge > float(bat["max_discharge_kwh_per_hour"]) + TOL:
            errs.append(f"h{h}: discharge rate exceeded")
        if h in no_charge and charge > TOL:
            errs.append(f"h{h}: charged inside no_charge_window")
        if h in no_discharge and discharge > TOL:
            errs.append(f"h{h}: discharged inside no_discharge_window")
        if h in grid_cap and g > grid_cap[h] + TOL:
            errs.append(f"h{h}: grid {g} exceeds cap {grid_cap[h]}")

        if abs((g + s + discharge) - (float(hours[h]["demand_kwh"]) + charge)) > TOL:
            errs.append(f"h{h}: energy balance violated")

        energy += charge - discharge
        if abs(energy - float(p["battery_energy_after_kwh"])) > TOL:
            errs.append(f"h{h}: battery_energy_after_kwh inconsistent")
        energy = float(p["battery_energy_after_kwh"])
        if energy < active_min[h] - TOL:
            errs.append(f"h{h}: below active minimum {active_min[h]}")
        if energy > float(bat["capacity_kwh"]) + TOL:
            errs.append(f"h{h}: above capacity")

        total_grid += g
        total_cost += g * float(hours[h]["tariff_bdt_per_kwh"])
        peak = max(peak, g)

    if abs(energy - float(bat["initial_energy_kwh"])) > TOL:
        errs.append(f"end-of-day battery {energy} != initial {bat['initial_energy_kwh']}")
    if abs(total_grid - float(resp.get("total_grid_kwh", -1))) > TOL:
        errs.append("total_grid_kwh does not match hourly_plan")
    if abs(total_cost - float(resp.get("total_cost_bdt", -1))) > TOL:
        errs.append("total_cost_bdt does not match hourly_plan")
    if abs(peak - float(resp.get("peak_grid_kwh", -1))) > TOL:
        errs.append("peak_grid_kwh does not match hourly_plan")
    return errs


def compare_interpretation(expected: List[Dict[str, Any]], got: Any,
                           n_notes: int) -> List[str]:
    errs: List[str] = []
    if not isinstance(got, list) or len(got) != n_notes:
        return [f"expected {n_notes} interpretation entries, got "
                f"{len(got) if isinstance(got, list) else type(got).__name__}"]
    if [e.get("note_index") for e in got] != list(range(n_notes)):
        errs.append("note_index values are not 0..N-1 in order")
    exp_by_idx = {e["note_index"]: e for e in expected}
    for e in got:
        ref = exp_by_idx.get(e.get("note_index"))
        if ref is None:
            continue
        i = e.get("note_index")
        if e.get("directive_type") != ref["directive_type"]:
            errs.append(f"note {i}: type {e.get('directive_type')!r} != {ref['directive_type']!r}")
            continue
        if bool(e.get("applies")) != bool(ref["applies"]):
            errs.append(f"note {i}: applies {e.get('applies')} != {ref['applies']}")
        ra, ea = ref["structured_adjustment"], e.get("structured_adjustment")
        if ra is None:
            if ea is not None:
                errs.append(f"note {i}: structured_adjustment must be null for no_op")
            continue
        if not isinstance(ea, dict):
            errs.append(f"note {i}: missing structured_adjustment")
            continue
        if list(ea.get("hours", [])) != list(ra["hours"]):
            errs.append(f"note {i}: hours {ea.get('hours')} != {ra['hours']}")
        for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
            if key in ra:
                if key not in ea:
                    errs.append(f"note {i}: missing {key}")
                elif abs(float(ea[key]) - float(ra[key])) > TOL:
                    errs.append(f"note {i}: {key} {ea[key]} != {ra[key]}")
    return errs


# ------------------------------------------------------------------------- runners


def run_offline(case: Dict[str, Any]) -> Tuple[Dict[str, Any], float]:
    import optimizer
    inp = case["input"]
    directives = case["expected_output"]["directive_interpretation"]
    t0 = time.perf_counter()
    out = optimizer.optimize(inp["hours"], inp["battery"], directives)
    dt = time.perf_counter() - t0
    out["scenario_id"] = inp["scenario_id"]
    out["directive_interpretation"] = directives
    out["plan_summary"] = "offline optimizer check"
    return out, dt


def run_http(case: Dict[str, Any], base_url: str) -> Tuple[Dict[str, Any], float]:
    import urllib.request
    body = json.dumps(case["input"]).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/optimize-energy", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    return out, time.perf_counter() - t0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default=None)
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--cases", default=os.path.join(HERE, "public_cases.json"))
    args = ap.parse_args()
    if not args.offline and not args.base_url:
        args.base_url = "http://localhost:8000"

    with open(args.cases, encoding="utf-8") as f:
        cases = json.load(f)["cases"]

    interp_ok = valid_ok = 0
    ratios: List[float] = []
    latencies: List[float] = []

    print(f"{'case':<11} {'interp':<7} {'valid':<6} {'cost':>10} {'ref':>10} "
          f"{'ratio':>6} {'ms':>7}  notes")
    print("-" * 78)

    for case in cases:
        inp = case["input"]
        exp = case["expected_output"]
        try:
            resp, dt = (run_offline(case) if args.offline
                        else run_http(case, args.base_url))
        except Exception as exc:  # noqa: BLE001
            print(f"{case['id']:<11} REQUEST FAILED: {exc}")
            continue
        latencies.append(dt)

        ierrs = ([] if args.offline else
                 compare_interpretation(exp["directive_interpretation"],
                                        resp.get("directive_interpretation"),
                                        len(inp["operator_notes"])))
        # replay against ORGANISER ground truth, not our own interpretation
        verrs = replay(inp, resp, exp["directive_interpretation"])

        cost = float(resp.get("total_cost_bdt", 0) or 0)
        ref = float(exp["total_cost_bdt"])
        ratio = min(1.0, ref / cost) if cost > 0 else 0.0
        if not verrs:
            valid_ok += 1
            ratios.append(ratio)
        else:
            ratios.append(0.0)
        if not ierrs:
            interp_ok += 1

        msg = "; ".join((ierrs + verrs)[:2])
        print(f"{case['id']:<11} {'OK' if not ierrs else 'FAIL':<7} "
              f"{'OK' if not verrs else 'FAIL':<6} {cost:>10.2f} {ref:>10.2f} "
              f"{ratio:>6.3f} {dt * 1000:>7.1f}  {msg}")

    n = len(cases)
    avg_ratio = sum(ratios) / n if n else 0.0
    lat = sorted(latencies)
    p95 = lat[int(len(lat) * 0.95) - 1] if lat else 0.0
    print("-" * 78)
    print(f"interpretation {interp_ok}/{n}   valid {valid_ok}/{n}   "
          f"avg cost ratio {avg_ratio:.4f}   p95 {p95 * 1000:.0f} ms")
    print(f"rubric estimate: interpretation 25pt -> {25 * interp_ok / n:.1f}, "
          f"application 25pt -> {25 * valid_ok / n:.1f}, "
          f"optimization 10pt -> {10 * avg_ratio:.1f}")
    return 0 if (valid_ok == n and interp_ok == n) else 1


if __name__ == "__main__":
    sys.exit(main())
