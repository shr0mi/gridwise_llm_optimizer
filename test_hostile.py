"""Malformed and hostile request drill.

Participant Guide, Performance & Reliability (2 pts): "Malformed JSON, invalid
structured input, LLM/provider errors, repeated requests, and unexpected valid
numeric combinations do not crash the service."

Every case here must come back as a controlled 400 or 422 -- never a 500, never
a dropped connection, never a stack trace or a secret in the body.

  python test_hostile.py --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

BATTERY = {
    "capacity_kwh": 500.0,
    "initial_energy_kwh": 200.0,
    "minimum_energy_kwh": 50.0,
    "max_charge_kwh_per_hour": 100.0,
    "max_discharge_kwh_per_hour": 100.0,
}


def hours(n: int = 24) -> List[Dict[str, Any]]:
    return [{"hour": h, "demand_kwh": 200.0, "solar_kwh": 50.0,
             "tariff_bdt_per_kwh": 9.0} for h in range(n)]


def valid_body() -> Dict[str, Any]:
    return {"scenario_id": "HOSTILE-OK", "operator_notes": ["The cafeteria menu changes."],
            "hours": hours(), "battery": dict(BATTERY)}


def _mutate(**changes: Any) -> Dict[str, Any]:
    body = valid_body()
    body.update(changes)
    return body


# (name, raw body bytes, accepted status codes)
CASES: List[Tuple[str, bytes, Tuple[int, ...]]] = [
    ("truncated JSON", b'{"scenario_id": "X", "operator_notes": [', (400,)),
    ("not JSON at all", b"this is not json", (400,)),
    ("empty body", b"", (400,)),
    ("JSON array instead of object", b"[1, 2, 3]", (400,)),
    ("JSON null", b"null", (400,)),

    ("missing scenario_id",
     json.dumps({k: v for k, v in valid_body().items() if k != "scenario_id"}).encode(),
     (400,)),
    ("missing battery",
     json.dumps({k: v for k, v in valid_body().items() if k != "battery"}).encode(),
     (400,)),
    ("zero operator notes", json.dumps(_mutate(operator_notes=[])).encode(), (400,)),
    ("four operator notes",
     json.dumps(_mutate(operator_notes=["a", "b", "c", "d"])).encode(), (400,)),
    ("blank operator note", json.dumps(_mutate(operator_notes=["   "])).encode(), (400,)),
    ("operator note is a number", json.dumps(_mutate(operator_notes=[42])).encode(),
     (400,)),
    ("23 hours", json.dumps(_mutate(hours=hours(23))).encode(), (400,)),
    ("25 hours", json.dumps(_mutate(hours=hours(25))).encode(), (400,)),
    ("duplicate hour index",
     json.dumps(_mutate(hours=hours(23) + [{"hour": 0, "demand_kwh": 1.0,
                                            "solar_kwh": 0.0,
                                            "tariff_bdt_per_kwh": 1.0}])).encode(),
     (400,)),
    ("hour out of range",
     json.dumps(_mutate(hours=[{**h, "hour": 99} if i == 0 else h
                               for i, h in enumerate(hours())])).encode(), (400,)),
    ("negative demand",
     json.dumps(_mutate(hours=[{**h, "demand_kwh": -5.0} if i == 3 else h
                               for i, h in enumerate(hours())])).encode(), (400,)),
    ("demand is a string",
     json.dumps(_mutate(hours=[{**h, "demand_kwh": "lots"} if i == 3 else h
                               for i, h in enumerate(hours())])).encode(), (400,)),
    ("battery is null", json.dumps(_mutate(battery=None)).encode(), (400,)),
    ("battery capacity zero",
     json.dumps(_mutate(battery={**BATTERY, "capacity_kwh": 0})).encode(), (400,)),
    ("battery field missing",
     json.dumps(_mutate(battery={k: v for k, v in BATTERY.items()
                                 if k != "max_charge_kwh_per_hour"})).encode(), (400,)),
    ("NaN demand", b'{"scenario_id":"X","operator_notes":["n"],"hours":[{"hour":0,'
                   b'"demand_kwh":NaN,"solar_kwh":0,"tariff_bdt_per_kwh":1}],'
                   b'"battery":' + json.dumps(BATTERY).encode() + b"}", (400, 422)),

    # well-formed, but the numbers contradict each other -> semantic
    ("initial energy above capacity",
     json.dumps(_mutate(battery={**BATTERY, "initial_energy_kwh": 900.0})).encode(),
     (400, 422)),
    ("minimum above capacity",
     json.dumps(_mutate(battery={**BATTERY, "minimum_energy_kwh": 900.0})).encode(),
     (400, 422)),
    ("initial below minimum",
     json.dumps(_mutate(battery={**BATTERY, "initial_energy_kwh": 10.0})).encode(),
     (400, 422)),

    # valid but unusual -- must succeed, not error
    ("huge but valid numbers",
     json.dumps(_mutate(
         hours=[{"hour": h, "demand_kwh": 1e6, "solar_kwh": 1e5,
                 "tariff_bdt_per_kwh": 1e3} for h in range(24)],
         battery={"capacity_kwh": 1e7, "initial_energy_kwh": 5e6,
                  "minimum_energy_kwh": 0.0, "max_charge_kwh_per_hour": 1e6,
                  "max_discharge_kwh_per_hour": 1e6})).encode(), (200,)),
    ("all-zero scenario",
     json.dumps(_mutate(
         hours=[{"hour": h, "demand_kwh": 0.0, "solar_kwh": 0.0,
                 "tariff_bdt_per_kwh": 0.0} for h in range(24)],
         battery={"capacity_kwh": 1.0, "initial_energy_kwh": 0.0,
                  "minimum_energy_kwh": 0.0, "max_charge_kwh_per_hour": 0.0,
                  "max_discharge_kwh_per_hour": 0.0})).encode(), (200,)),
    ("extra unknown fields are ignored",
     json.dumps({**valid_body(), "nonsense": {"deeply": ["nested", 1, None]}}).encode(),
     (200,)),
    ("unicode and very long note",
     json.dumps(_mutate(operator_notes=["সৌর " * 500 + " drop to 50% from 1 PM to 3 PM"])).encode(),
     (200, 400)),
]

_LEAK_MARKERS = ("Traceback", "File \"", "GEMINI_API_KEY", "AIza", "api_key",
                 "system_instruction", "google.genai")


def post(base_url: str, body: bytes) -> Tuple[int, str]:
    req = urllib.request.Request(base_url.rstrip("/") + "/optimize-energy",
                                 data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    args = ap.parse_args()

    passed = failures = 0
    print(f"{'case':<36} {'code':>5}  result")
    print("-" * 72)
    for name, body, accepted in CASES:
        try:
            code, text = post(args.base_url, body)
        except Exception as exc:  # noqa: BLE001
            print(f"{name:<36} {'---':>5}  CONNECTION FAILED: {exc}")
            failures += 1
            continue

        problems = []
        if code not in accepted:
            problems.append(f"expected {accepted}")
        if code >= 500:
            problems.append("server error")
        leaked = [m for m in _LEAK_MARKERS if m in text]
        if leaked:
            problems.append(f"leaked {leaked[0]!r}")

        if problems:
            failures += 1
            print(f"{name:<36} {code:>5}  FAIL: {'; '.join(problems)}")
        else:
            passed += 1
            print(f"{name:<36} {code:>5}  ok")

    # repeated identical requests must stay stable
    body = json.dumps(valid_body()).encode()
    codes = {post(args.base_url, body)[0] for _ in range(5)}
    if codes == {200}:
        passed += 1
        print(f"{'5x repeated identical request':<36} {200:>5}  ok")
    else:
        failures += 1
        print(f"{'5x repeated identical request':<36} {'---':>5}  FAIL: saw {codes}")

    print("-" * 72)
    print(f"{passed} passed, {failures} failed")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
