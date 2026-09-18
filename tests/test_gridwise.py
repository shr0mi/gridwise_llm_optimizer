"""Pytest regression gate for GridWise.

The standalone scripts (`test_local.py`, `test_paraphrase.py`, `test_hostile.py`)
still work as CLIs, but pytest collected nothing from them, so the suite only ran
if someone remembered all three commands with the right flags. These wrap the
same assertions as parametrized tests, so one `pytest` run covers everything and
a failure names the exact case.

    pytest -q                      # offline: optimizer, extractor, guardrails
    pytest -q --base-url URL       # adds the live HTTP contract tests

Nothing here needs an API key or a network unless --base-url is given.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import llm            # noqa: E402
import optimizer      # noqa: E402
import replay         # noqa: E402
import rules          # noqa: E402
from schemas import ScenarioRequest  # noqa: E402

TOL = 0.01


def _load(name: str) -> Dict[str, Any]:
    with open(ROOT / name, encoding="utf-8") as f:
        return json.load(f)


PUBLIC = _load("public_cases.json")["cases"]
PACK = _load("test.json")["cases"] if (ROOT / "test.json").exists() else []
SCORED = [c for c in PACK if "input" in c and c["expectations"]["match_cost"]]
ROBUST = [c for c in PACK if "input" in c and not c["expectations"]["match_cost"]]
INVALID = [c for c in PACK if "raw_body" in c]


def pytest_configure(config):  # pragma: no cover - pytest hook
    pass


# ---------------------------------------------------------------- the optimizer


@pytest.mark.parametrize("case", PUBLIC, ids=[c["id"] for c in PUBLIC])
def test_public_case_matches_organizer_optimal(case):
    """The LP must reproduce the organizer's reference cost exactly."""
    inp = case["input"]
    truth = case["expected_output"]["directive_interpretation"]
    out = optimizer.optimize(inp["hours"], inp["battery"], truth)
    out.pop("tier", None)
    assert not replay.validate(inp["hours"], inp["battery"], truth, out)
    assert abs(out["total_cost_bdt"]
               - float(case["expected_output"]["total_cost_bdt"])) <= TOL


@pytest.mark.parametrize("case", SCORED, ids=[c["id"] for c in SCORED])
def test_pack_case_is_optimal_and_valid(case):
    """Every scored case in test.json solves to its recorded optimum."""
    inp = case["input"]
    truth = case["expected_output"]["directive_interpretation"]
    out = optimizer.optimize(inp["hours"], inp["battery"], truth)
    assert out.pop("tier", None) == "optimal"
    assert not replay.validate(inp["hours"], inp["battery"], truth, out)
    assert abs(out["total_cost_bdt"]
               - float(case["expected_output"]["total_cost_bdt"])) <= TOL


@pytest.mark.parametrize("case", ROBUST, ids=[c["id"] for c in ROBUST])
def test_contradictory_directives_still_produce_a_plan(case):
    """Unsatisfiable directives must never crash or emit a malformed plan."""
    inp = case["input"]
    truth = case["expected_output"]["directive_interpretation"]
    out = optimizer.optimize(inp["hours"], inp["battery"], truth)
    out.pop("tier", None)
    assert len(out["hourly_plan"]) == 24
    assert sorted(p["hour"] for p in out["hourly_plan"]) == list(range(24))
    # The plan must at minimum obey the base GridWise rules.
    assert not replay.validate(inp["hours"], inp["battery"], [], out)


def test_replay_rejects_a_tampered_plan():
    inp = PUBLIC[0]["input"]
    truth = PUBLIC[0]["expected_output"]["directive_interpretation"]
    out = optimizer.optimize(inp["hours"], inp["battery"], truth)
    out.pop("tier", None)
    tampered = json.loads(json.dumps(out))
    tampered["hourly_plan"][5]["grid_kwh"] += 10
    assert replay.validate(inp["hours"], inp["battery"], truth, tampered)


def test_replay_rejects_broken_neutrality():
    inp = PUBLIC[0]["input"]
    truth = PUBLIC[0]["expected_output"]["directive_interpretation"]
    out = optimizer.optimize(inp["hours"], inp["battery"], truth)
    out.pop("tier", None)
    broken = json.loads(json.dumps(out))
    broken["hourly_plan"][23]["battery_energy_after_kwh"] += 5
    assert replay.validate(inp["hours"], inp["battery"], truth, broken)


# ------------------------------------------------------------- the interpreter

_PARA_CASES: List[tuple] = []
try:
    from test_paraphrase import BATTERY as PARA_BATTERY, CASES as PARA_CASES
    _PARA_CASES = list(PARA_CASES)
except Exception:  # pragma: no cover
    PARA_BATTERY = {}


@pytest.mark.parametrize("note,want_type,want_adj", _PARA_CASES,
                         ids=[c[0][:58] for c in _PARA_CASES])
def test_paraphrase_extraction(note, want_type, want_adj):
    """Hidden cases reword the same directives; the rule path must keep up."""
    got = rules.extract_one(note, PARA_BATTERY)
    assert got["directive_type"] == want_type
    assert bool(got["applies"]) is (want_type != "no_op")
    adj = got["structured_adjustment"]
    if want_adj is None:
        assert adj is None
        return
    assert adj["hours"] == list(want_adj["hours"])
    for key, value in want_adj.items():
        if key != "hours":
            assert abs(float(adj[key]) - float(value)) <= TOL


@pytest.mark.parametrize("case", PUBLIC, ids=[c["id"] for c in PUBLIC])
def test_rule_extractor_on_public_notes(case):
    inp = case["input"]
    got = rules.extract(inp["operator_notes"], inp["battery"])
    want = case["expected_output"]["directive_interpretation"]
    for g, w in zip(got, want):
        assert g["directive_type"] == w["directive_type"]
        if w["structured_adjustment"] is None:
            assert g["structured_adjustment"] is None
        else:
            assert g["structured_adjustment"]["hours"] == w["structured_adjustment"]["hours"]


# The four gaps found in code review. Each was a real miss.
REVIEW_CASES = [
    ("Battery must hold 60% of capacity from 7 PM to 10 PM.",
     "minimum_battery_reserve", [19, 20, 21], "minimum_energy_kwh", 120.0),
    ("Hold no fewer than 95 kWh in the pack from 5 PM to 8 PM.",
     "minimum_battery_reserve", [17, 18, 19], "minimum_energy_kwh", 95.0),
    ("Please avoid drawing down the pack from 16:00 to 18:00.",
     "no_discharge_window", [16, 17], None, None),
    # The dangerous one: "offline" must not override the stated fraction, or we
    # apply a wrong constraint instead of merely missing one.
    ("Half the array is offline for rewiring from 9 AM to 11 AM.",
     "solar_reduction", [9, 10], "factor", 0.5),
]


@pytest.mark.parametrize("note,want_type,want_hours,key,value", REVIEW_CASES,
                         ids=[c[0][:50] for c in REVIEW_CASES])
def test_review_paraphrase_gaps(note, want_type, want_hours, key, value):
    battery = {"capacity_kwh": 200.0, "initial_energy_kwh": 120.0,
               "minimum_energy_kwh": 40.0, "max_charge_kwh_per_hour": 60.0,
               "max_discharge_kwh_per_hour": 60.0}
    got = rules.extract_one(note, battery)
    assert got["directive_type"] == want_type
    assert got["structured_adjustment"]["hours"] == want_hours
    if key:
        assert abs(float(got["structured_adjustment"][key]) - value) <= TOL


# ---------------------------------------------------------------- guardrails

BATTERY = {"capacity_kwh": 200.0, "initial_energy_kwh": 120.0,
           "minimum_energy_kwh": 40.0, "max_charge_kwh_per_hour": 60.0,
           "max_discharge_kwh_per_hour": 60.0}

HOSTILE_MODEL_OUTPUT = [
    pytest.param(None, id="null"),
    pytest.param([], id="empty-list"),
    pytest.param("not a list", id="string"),
    pytest.param([{"note_index": 99, "directive_type": "solar_reduction"}], id="bad-index"),
    pytest.param([{"note_index": 0, "directive_type": "teleport_energy"}], id="invented-type"),
    pytest.param([{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
                   "structured_adjustment": {"hours": [99, -3], "factor": 0.2}}],
                 id="out-of-range-hours"),
    pytest.param([{"note_index": 0, "applies": True, "directive_type": "solar_reduction",
                   "structured_adjustment": {"hours": [3, 1, 2], "factor": 7.5}}],
                 id="factor-out-of-range"),
    pytest.param([{"note_index": 0, "applies": True,
                   "directive_type": "minimum_battery_reserve",
                   "structured_adjustment": {"hours": [1], "minimum_energy_kwh": 1e9}}],
                 id="reserve-above-capacity"),
    pytest.param([{"note_index": 0, "applies": False,
                   "directive_type": "no_charge_window",
                   "structured_adjustment": {"hours": [1]}}], id="applies-false-non-noop"),
    pytest.param([{"note_index": 0}, {"note_index": 0}], id="duplicate-index"),
]


@pytest.mark.parametrize("raw", HOSTILE_MODEL_OUTPUT)
def test_guardrails_never_emit_an_invalid_directive(raw):
    """Model output is untrusted. Whatever arrives, the result must be legal."""
    notes = ["some operator note", "another one"]
    out = llm.sanitize(raw, notes, BATTERY)
    assert len(out) == len(notes)
    for i, entry in enumerate(out):
        assert entry["note_index"] == i
        assert entry["directive_type"] in llm.ALLOWED
        if entry["directive_type"] == "no_op":
            assert entry["applies"] is False
            assert entry["structured_adjustment"] is None
        else:
            assert entry["applies"] is True
            adj = entry["structured_adjustment"]
            hrs = adj["hours"]
            assert hrs == sorted(set(hrs)) and all(0 <= h <= 23 for h in hrs)
            if "factor" in adj:
                assert 0.0 <= adj["factor"] <= 1.0
            if "minimum_energy_kwh" in adj:
                assert 0.0 <= adj["minimum_energy_kwh"] <= BATTERY["capacity_kwh"]
            if "max_grid_kwh" in adj:
                assert adj["max_grid_kwh"] >= 0.0


# ------------------------------------------------------------ request schema

def _valid_payload() -> Dict[str, Any]:
    return json.loads(json.dumps(PUBLIC[0]["input"]))


@pytest.mark.parametrize("field", ["demand_kwh", "solar_kwh", "tariff_bdt_per_kwh"])
@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_hour_values_are_rejected(field, value):
    """Pydantic floats accept inf unless told otherwise, and +inf passes ge=0."""
    payload = _valid_payload()
    payload["hours"][3][field] = value
    with pytest.raises(Exception):
        ScenarioRequest.model_validate(payload)


@pytest.mark.parametrize("field", ["capacity_kwh", "initial_energy_kwh",
                                   "minimum_energy_kwh", "max_charge_kwh_per_hour",
                                   "max_discharge_kwh_per_hour"])
def test_non_finite_battery_values_are_rejected(field):
    payload = _valid_payload()
    payload["battery"][field] = float("inf")
    with pytest.raises(Exception):
        ScenarioRequest.model_validate(payload)


def test_valid_payload_parses():
    assert ScenarioRequest.model_validate(_valid_payload())


@pytest.mark.parametrize("mutate,label", [
    (lambda p: p.update(operator_notes=[]), "zero-notes"),
    (lambda p: p.update(operator_notes=["a", "b", "c", "d"]), "four-notes"),
    (lambda p: p.update(operator_notes=["  "]), "blank-note"),
    (lambda p: p.update(hours=p["hours"][:23]), "23-hours"),
    (lambda p: p["hours"].__setitem__(0, {**p["hours"][0], "hour": 5}), "duplicate-hour"),
    (lambda p: p["battery"].update(capacity_kwh=0), "zero-capacity"),
    (lambda p: p["battery"].update(initial_energy_kwh=1e9), "initial-above-capacity"),
], ids=lambda x: x if isinstance(x, str) else "")
def test_structurally_invalid_payloads_are_rejected(mutate, label):
    payload = _valid_payload()
    mutate(payload)
    with pytest.raises(Exception):
        ScenarioRequest.model_validate(payload)


# ------------------------------------------------------------- live HTTP tests

def _base_url() -> str:
    return os.getenv("GRIDWISE_BASE_URL", "").rstrip("/")


live = pytest.mark.skipif(not _base_url(),
                          reason="set GRIDWISE_BASE_URL to run live HTTP tests")


@live
def test_health_endpoint():
    import urllib.request
    with urllib.request.urlopen(_base_url() + "/health", timeout=10) as r:
        assert r.status == 200
        assert json.loads(r.read())["status"] == "ok"


@live
@pytest.mark.parametrize("case", PUBLIC, ids=[c["id"] for c in PUBLIC])
def test_live_public_case(case):
    import urllib.request
    body = json.dumps(case["input"]).encode()
    req = urllib.request.Request(_base_url() + "/optimize-energy", data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        resp = json.loads(r.read())

    inp = case["input"]
    truth = case["expected_output"]["directive_interpretation"]
    assert resp["scenario_id"] == inp["scenario_id"]
    assert set(resp) == {"scenario_id", "directive_interpretation", "hourly_plan",
                         "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh",
                         "plan_summary"}
    assert len(resp["directive_interpretation"]) == len(inp["operator_notes"])
    assert not replay.validate(inp["hours"], inp["battery"], truth, resp)
    assert abs(resp["total_cost_bdt"]
               - float(case["expected_output"]["total_cost_bdt"])) <= TOL


@live
def test_llm_path_is_actually_used():
    """A perfect score proves nothing if the model never ran."""
    import urllib.request
    with urllib.request.urlopen(_base_url() + "/diagnostics", timeout=10) as r:
        diag = json.loads(r.read())
    assert diag["interpreted_by_fallback"] == 0, (
        f"the deterministic parser served {diag['interpreted_by_fallback']} "
        f"request(s); last error: {diag['last_llm_error']}")
    assert diag["interpreted_by_llm"] > 0, "no request reached the language model"
