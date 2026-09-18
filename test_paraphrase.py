"""Paraphrase-robustness suite.

The hidden judge set rewords the same directives (Problem Statement 11.4), so
this file checks interpretation against hand-written rephrasings rather than the
public wording. It is the direct proxy for the 5 paraphrase-robustness points.

  python test_paraphrase.py                 # deterministic extractor only (no key)
  python test_paraphrase.py --llm           # live model + guardrails (needs a key)
  python test_paraphrase.py --base-url URL  # through the deployed service
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any, Dict, List, Optional, Tuple

BATTERY = {
    "capacity_kwh": 200.0,
    "initial_energy_kwh": 120.0,
    "minimum_energy_kwh": 40.0,
    "max_charge_kwh_per_hour": 60.0,
    "max_discharge_kwh_per_hour": 60.0,
}

TOL = 0.01

# (note, expected_type, expected_adjustment or None)
CASES: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    # ---- solar_reduction: clock formats -------------------------------------
    ("Solar output will drop to about 20% from 1 PM to 3 PM.",
     "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
    ("PV production will drop to about 20% between 13:00 and 15:00.",
     "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
    ("Expect an 80% reduction in rooftop solar during the 1-3 PM maintenance window.",
     "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
    ("Panel washing from one until three will leave roughly one-fifth of normal solar output.",
     "solar_reduction", {"hours": [13, 14], "factor": 0.2}),
    ("Cloud cover will leave about half of the forecast solar output from 10 AM until noon.",
     "solar_reduction", {"hours": [10, 11], "factor": 0.5}),
    ("Array derated by 75 percent between 11:00 and 14:00 for inverter work.",
     "solar_reduction", {"hours": [11, 12, 13], "factor": 0.25}),
    ("The panels are offline from 9 AM until 11 AM.",
     "solar_reduction", {"hours": [9, 10], "factor": 0.0}),
    ("Rooftop generation runs at roughly 40% of normal from 8 AM to 10 AM.",
     "solar_reduction", {"hours": [8, 9], "factor": 0.4}),
    ("Solar will be cut by two-thirds from 12 PM to 2 PM.",
     "solar_reduction", {"hours": [12, 13], "factor": 1 / 3}),
    ("Between 2 PM and 5 PM expect only a quarter of usual PV output.",
     "solar_reduction", {"hours": [14, 15, 16], "factor": 0.25}),

    # ---- no_charge_window ----------------------------------------------------
    ("Do not charge the battery between 2 PM and 4 PM.",
     "no_charge_window", {"hours": [14, 15]}),
    ("The battery charger will be isolated from 2 AM until 5 AM for maintenance.",
     "no_charge_window", {"hours": [2, 3, 4]}),
    ("The charging circuit will be unavailable from 2 PM until 4 PM.",
     "no_charge_window", {"hours": [14, 15]}),
    ("No charging is permitted from 11 AM until 1 PM.",
     "no_charge_window", {"hours": [11, 12]}),
    ("Battery charging stays locked out between 01:00 and 04:00.",
     "no_charge_window", {"hours": [1, 2, 3]}),
    ("Suspend charging from 10 PM until midnight.",
     "no_charge_window", {"hours": [22, 23]}),
    ("Charging must not happen from 10 PM until 2 AM.",
     "no_charge_window", {"hours": [0, 1, 22, 23]}),

    # ---- no_discharge_window -------------------------------------------------
    ("For protection testing, the battery must not discharge from 6 PM until 8 PM.",
     "no_discharge_window", {"hours": [18, 19]}),
    ("Do not discharge the battery from 5 PM until 7 PM during relay testing.",
     "no_discharge_window", {"hours": [17, 18]}),
    ("No battery export between 17:00 and 19:00.",
     "no_discharge_window", {"hours": [17, 18]}),
    ("Battery discharging is disabled from 7 PM through 9 PM.",
     "no_discharge_window", {"hours": [19, 20]}),
    ("The battery may not supply load from 4 PM to 6 PM.",
     "no_discharge_window", {"hours": [16, 17]}),

    # ---- minimum_battery_reserve --------------------------------------------
    ("Keep at least 120 kWh in reserve from 6 PM until 9 PM.",
     "minimum_battery_reserve", {"hours": [18, 19, 20], "minimum_energy_kwh": 120}),
    ("Keep at least 50% of the battery capacity stored from 6 PM until 9 PM.",
     "minimum_battery_reserve", {"hours": [18, 19, 20], "minimum_energy_kwh": 100}),
    ("The data center requires at least 80 kWh to remain in the battery from 6 PM until 10 PM.",
     "minimum_battery_reserve", {"hours": [18, 19, 20, 21], "minimum_energy_kwh": 80}),
    ("Maintain a battery floor of 90 kWh between 19:00 and 22:00.",
     "minimum_battery_reserve", {"hours": [19, 20, 21], "minimum_energy_kwh": 90}),
    ("Battery state of charge must not fall below 75 kWh from 8 PM until 11 PM.",
     "minimum_battery_reserve", {"hours": [20, 21, 22], "minimum_energy_kwh": 75}),
    ("Hold back a quarter of the pack from 7 PM until 10 PM.",
     "minimum_battery_reserve", {"hours": [19, 20, 21], "minimum_energy_kwh": 50}),
    ("Keep 60 kWh in reserve throughout the day.",
     "minimum_battery_reserve", {"hours": list(range(24)), "minimum_energy_kwh": 60}),

    # ---- max_grid_window -----------------------------------------------------
    ("From 6 PM until 9 PM, campus grid import must not exceed 155 kWh in any hour.",
     "max_grid_window", {"hours": [18, 19, 20], "max_grid_kwh": 155}),
    ("The evening transformer limit is 180 kWh of grid import from 7 PM until 9 PM.",
     "max_grid_window", {"hours": [19, 20], "max_grid_kwh": 180}),
    ("Grid intake must stay at or below 190 kWh from 7 PM until 10 PM.",
     "max_grid_window", {"hours": [19, 20, 21], "max_grid_kwh": 190}),
    ("Cap grid draw at 150 kWh during the 7 PM hour.",
     "max_grid_window", {"hours": [19], "max_grid_kwh": 150}),
    ("Substation works restrict import to no more than 165 kWh between 18:00 and 21:00.",
     "max_grid_window", {"hours": [18, 19, 20], "max_grid_kwh": 165}),
    ("Utility draw is limited to a maximum of 200 kWh from 8 PM until 11 PM.",
     "max_grid_window", {"hours": [20, 21, 22], "max_grid_kwh": 200}),

    # ---- no_op distractors ---------------------------------------------------
    ("The cafeteria menu changes tomorrow.", "no_op", None),
    ("The sports office moved next month's registration deadline.", "no_op", None),
    ("The library is extending book-return hours next week.", "no_op", None),
    ("A seminar room booking was moved to next week.", "no_op", None),
    ("The student affairs office will publish club notices tomorrow.", "no_op", None),
    ("We are evaluating new solar panels for installation next quarter.", "no_op", None),
    ("The gym will publish its new class timetable on Monday.", "no_op", None),
    ("Campus wifi maintenance is scheduled for the weekend.", "no_op", None),
]


def check(idx: int, note: str, want_type: str, want_adj: Optional[Dict[str, Any]],
          got: Dict[str, Any]) -> List[str]:
    errs: List[str] = []
    if got.get("directive_type") != want_type:
        return [f"type {got.get('directive_type')!r} != {want_type!r}"]
    if bool(got.get("applies")) != (want_type != "no_op"):
        errs.append(f"applies={got.get('applies')} is wrong for {want_type}")
    adj = got.get("structured_adjustment")
    if want_adj is None:
        if adj is not None:
            errs.append("structured_adjustment must be null for no_op")
        return errs
    if not isinstance(adj, dict):
        return errs + ["structured_adjustment missing"]
    if list(adj.get("hours", [])) != list(want_adj["hours"]):
        errs.append(f"hours {adj.get('hours')} != {want_adj['hours']}")
    for key, value in want_adj.items():
        if key == "hours":
            continue
        if key not in adj:
            errs.append(f"missing {key}")
        elif abs(float(adj[key]) - float(value)) > TOL:
            errs.append(f"{key} {adj[key]} != {value}")
    return errs


def interpret_rules() -> List[Dict[str, Any]]:
    import rules
    return [rules.extract_one(note, BATTERY) for note, _, _ in CASES]


def interpret_llm() -> List[Dict[str, Any]]:
    import llm
    notes = [c[0] for c in CASES]
    hours = [{"hour": h, "demand_kwh": 150.0, "solar_kwh": 40.0 if 6 <= h <= 17 else 0.0,
              "tariff_bdt_per_kwh": 8.0} for h in range(24)]
    out: List[Dict[str, Any]] = []
    # one note per call keeps the mapping unambiguous for this suite
    for note in notes:
        res = asyncio.run(llm.interpret([note], hours, BATTERY))
        out.append(res[0])
    return out


def interpret_http(base_url: str) -> List[Dict[str, Any]]:
    import urllib.request
    hours = [{"hour": h, "demand_kwh": 150.0, "solar_kwh": 40.0 if 6 <= h <= 17 else 0.0,
              "tariff_bdt_per_kwh": 8.0} for h in range(24)]
    out: List[Dict[str, Any]] = []
    for i, (note, _, _) in enumerate(CASES):
        body = json.dumps({"scenario_id": f"PARA-{i:02d}", "operator_notes": [note],
                           "hours": hours, "battery": BATTERY}).encode()
        req = urllib.request.Request(base_url.rstrip("/") + "/optimize-energy",
                                     data=body,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            out.append(json.loads(r.read())["directive_interpretation"][0])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm", action="store_true", help="use the live model path")
    ap.add_argument("--base-url", default=None, help="test the deployed service")
    args = ap.parse_args()

    if args.base_url:
        got = interpret_http(args.base_url)
        label = f"service {args.base_url}"
    elif args.llm:
        got = interpret_llm()
        label = "llm + guardrails"
    else:
        got = interpret_rules()
        label = "deterministic extractor"

    passed = 0
    for i, ((note, want_type, want_adj), g) in enumerate(zip(CASES, got)):
        errs = check(i, note, want_type, want_adj, g)
        if errs:
            print(f"FAIL [{i:02d}] {note}")
            for e in errs:
                print(f"       {e}")
        else:
            passed += 1

    n = len(CASES)
    print("-" * 72)
    print(f"{label}: {passed}/{n} paraphrases correct ({100 * passed / n:.0f}%)")
    return 0 if passed == n else 1


if __name__ == "__main__":
    sys.exit(main())
