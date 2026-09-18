"""Operator-note interpretation: Gemini call + deterministic guardrails.

The language model is the interpreter. Everything it returns is treated as
untrusted structured data and must survive `sanitize()` before the optimizer is
allowed to see it. If the model is unavailable or returns something unusable the
service degrades to all-`no_op` rather than inventing a directive or failing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("gridwise.llm")

ALLOWED = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}

MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "12"))

_client = None
_client_ready = False


def _get_client():
    global _client, _client_ready
    if _client_ready:
        return _client
    _client_ready = True
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        log.warning("GEMINI_API_KEY is not set - interpretation will fall back to no_op")
        return None
    try:
        from google import genai
        _client = genai.Client(api_key=key)
    except Exception:  # noqa: BLE001
        log.exception("failed to construct the Gemini client")
        _client = None
    return _client


def llm_available() -> bool:
    return _get_client() is not None


# --------------------------------------------------------------------- the prompt

SYSTEM_INSTRUCTION = """You convert campus-operator notes into structured energy
directives for a 24-hour smart-campus scheduler. You are the interpretation
stage of a pipeline; downstream code validates everything you return.

Return one entry for EVERY note you are given, in note_index order starting at 0.

SUPPORTED DIRECTIVE TYPES AND THEIR EXACT structured_adjustment SHAPES
  solar_reduction          {"hours": [...], "factor": <0..1>}
  minimum_battery_reserve  {"hours": [...], "minimum_energy_kwh": <number>}
  no_charge_window         {"hours": [...]}
  no_discharge_window      {"hours": [...]}
  max_grid_window          {"hours": [...], "max_grid_kwh": <number>}
  no_op                    null

RULE 1 - TIME WINDOWS ARE START-INCLUSIVE AND END-EXCLUSIVE.
List every whole hour the window covers, starting at the start hour and stopping
BEFORE the end hour.
  "1 PM to 3 PM"            -> [13, 14]
  "noon until 2 PM"         -> [12, 13]
  "from 2 AM until 5 AM"    -> [2, 3, 4]
  "6 PM until 10 PM"        -> [18, 19, 20, 21]
  "between 11 AM and 2 PM"  -> [11, 12, 13]
  "13:00 to 15:00"          -> [13, 14]
  "10 PM until 2 AM"        -> [0, 1, 22, 23]      (wraps midnight; still ascending)
  "during the 7 PM hour"    -> [19]                 (a single hour)
  "throughout the day"      -> [0,1,2,...,23]       (all 24 hours)
  "until midnight" means the end hour is 24, so the last listed hour is 23.
hours must be unique integers 0-23 sorted in ASCENDING order.

RULE 2 - factor IS THE FRACTION THAT REMAINS USABLE, NOT THE REDUCTION.
  "an 80% reduction in solar"          -> factor 0.2
  "solar drops to about 25%"           -> factor 0.25
  "about half the forecast output"     -> factor 0.5
  "roughly one-fifth of normal output" -> factor 0.2
  "output cut by two thirds"           -> factor 0.33
  "panels offline / no solar"          -> factor 0.0

RULE 3 - RESOLVE RELATIVE QUANTITIES INTO ABSOLUTE kWh USING THE BATTERY DATA
GIVEN IN THE REQUEST. "keep at least 50% of battery capacity in reserve" with a
200 kWh battery means minimum_energy_kwh = 100.

RULE 4 - IRRELEVANT NOTES ARE no_op. Many notes are realistic distractors about
campus life (menus, bookings, deadlines, notices, events, next week, next month).
Anything that does not change today's 24-hour electricity schedule is:
applies = false, directive_type = "no_op", structured_adjustment = null.
A note is also no_op if it only describes something with no operational effect on
solar availability, battery charging, battery discharging, battery reserve, or
grid import limits.

RULE 5 - EVERY NON-no_op ENTRY MUST HAVE applies = true. Only no_op may use
applies = false.

RULE 6 - NEVER INVENT. Do not change demand, tariff, solar forecasts or battery
parameters, and never emit a directive_type outside the list above. If a note is
relevant but you cannot map it confidently onto exactly one supported type, use
no_op.

Each note maps to exactly ONE directive type. Keep `explanation` to one short
sentence.

WORKED EXAMPLES
Note: "Crews wash the array from 9 AM to 11 AM; expect about 40% of normal output."
  -> solar_reduction, applies true, {"hours": [9, 10], "factor": 0.4}
Note: "Hold back a third of the 300 kWh pack from 7 PM until 11 PM."
  -> minimum_battery_reserve, applies true, {"hours": [19,20,21,22], "minimum_energy_kwh": 100}
Note: "The charger stays locked out between 1 AM and 4 AM."
  -> no_charge_window, applies true, {"hours": [1, 2, 3]}
Note: "Relay tests mean no battery export from 5 PM to 7 PM."
  -> no_discharge_window, applies true, {"hours": [17, 18]}
Note: "Feeder works cap us at 140 kWh of import from 8 PM to 11 PM."
  -> max_grid_window, applies true, {"hours": [20, 21, 22], "max_grid_kwh": 140}
Note: "The gym will publish its new class timetable on Monday."
  -> no_op, applies false, null
"""

RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "interpretations": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "note_index": {"type": "INTEGER"},
                    "applies": {"type": "BOOLEAN"},
                    "directive_type": {
                        "type": "STRING",
                        "enum": sorted(ALLOWED),
                    },
                    "structured_adjustment": {
                        "type": "OBJECT",
                        "nullable": True,
                        "properties": {
                            "hours": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                            "factor": {"type": "NUMBER", "nullable": True},
                            "minimum_energy_kwh": {"type": "NUMBER", "nullable": True},
                            "max_grid_kwh": {"type": "NUMBER", "nullable": True},
                        },
                    },
                    "explanation": {"type": "STRING"},
                },
                "required": ["note_index", "applies", "directive_type", "explanation"],
            },
        }
    },
    "required": ["interpretations"],
}


def build_user_prompt(notes: Sequence[str], hours: Sequence[Dict[str, Any]],
                      battery: Dict[str, float]) -> str:
    lines = ["OPERATOR NOTES", ]
    for i, note in enumerate(notes):
        lines.append(f"  [{i}] {note}")
    lines.append("")
    lines.append("BATTERY (read-only facts; use for relative quantities)")
    lines.append(f"  capacity_kwh={battery['capacity_kwh']} "
                 f"initial_energy_kwh={battery['initial_energy_kwh']} "
                 f"minimum_energy_kwh={battery['minimum_energy_kwh']} "
                 f"max_charge_kwh_per_hour={battery['max_charge_kwh_per_hour']} "
                 f"max_discharge_kwh_per_hour={battery['max_discharge_kwh_per_hour']}")
    lines.append("")
    lines.append("HOURLY CONTEXT (read-only; hour | demand_kwh | solar_kwh | tariff)")
    for h in sorted(hours, key=lambda x: x["hour"]):
        lines.append(f"  {h['hour']:>2} | {h['demand_kwh']} | {h['solar_kwh']} | "
                     f"{h['tariff_bdt_per_kwh']}")
    lines.append("")
    lines.append(f"Return exactly {len(notes)} interpretations, note_index 0 to "
                 f"{len(notes) - 1}.")
    return "\n".join(lines)


# ----------------------------------------------------------------------- guardrails


def _finite(x: Any) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _clean_hours(raw: Any) -> List[int]:
    out = set()
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                h = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= h <= 23:
                out.add(h)
    return sorted(out)


def _no_op(idx: int, why: str = "This note does not affect today's 24-hour energy schedule.") -> Dict[str, Any]:
    return {
        "note_index": idx,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": why,
    }


def sanitize(raw: Any, notes: Sequence[str], battery: Dict[str, float]) -> List[Dict[str, Any]]:
    """Force model output into exactly one valid entry per note, in order.

    Anything that cannot be validated is demoted to no_op rather than dropped,
    so the response always carries `len(notes)` entries indexed 0..N-1.
    """
    n = len(notes)
    capacity = float(battery["capacity_kwh"])
    items = raw if isinstance(raw, list) else []
    by_index: Dict[int, Dict[str, Any]] = {}

    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("note_index"))
        except (TypeError, ValueError):
            continue
        if not 0 <= idx < n or idx in by_index:
            continue  # out of range, or a duplicate of one already accepted

        kind = item.get("directive_type")
        explanation = item.get("explanation")
        explanation = (explanation.strip() if isinstance(explanation, str) and explanation.strip()
                       else "Interpreted from the operator note.")
        explanation = explanation[:400]

        if kind not in ALLOWED or kind == "no_op":
            by_index[idx] = _no_op(idx, explanation if kind == "no_op"
                                   else "Unsupported interpretation; treated as no-op.")
            continue

        adj = item.get("structured_adjustment")
        if not isinstance(adj, dict):
            by_index[idx] = _no_op(idx, "Interpretation was incomplete; treated as no-op.")
            continue
        hrs = _clean_hours(adj.get("hours"))
        if not hrs:
            by_index[idx] = _no_op(idx, "No valid hours were extracted; treated as no-op.")
            continue

        clean: Dict[str, Any] = {"hours": hrs}
        if kind == "solar_reduction":
            f = _finite(adj.get("factor"))
            if f is None:
                by_index[idx] = _no_op(idx, "No usable solar factor; treated as no-op.")
                continue
            clean["factor"] = min(1.0, max(0.0, f))
        elif kind == "minimum_battery_reserve":
            v = _finite(adj.get("minimum_energy_kwh"))
            if v is None:
                by_index[idx] = _no_op(idx, "No usable reserve level; treated as no-op.")
                continue
            clean["minimum_energy_kwh"] = min(capacity, max(0.0, v))
        elif kind == "max_grid_window":
            v = _finite(adj.get("max_grid_kwh"))
            if v is None:
                by_index[idx] = _no_op(idx, "No usable grid cap; treated as no-op.")
                continue
            clean["max_grid_kwh"] = max(0.0, v)

        by_index[idx] = {
            "note_index": idx,
            "applies": True,          # only no_op may be applies = false
            "directive_type": kind,
            "structured_adjustment": clean,
            "explanation": explanation,
        }

    return [by_index.get(i) or _no_op(i) for i in range(n)]


# --------------------------------------------------------------------- the LLM call


async def _call_gemini(notes: Sequence[str], hours: Sequence[Dict[str, Any]],
                       battery: Dict[str, float]) -> Any:
    client = _get_client()
    if client is None:
        return None
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=SYSTEM_INSTRUCTION,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=RESPONSE_SCHEMA,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
        safety_settings=[
            types.SafetySetting(category=c, threshold="BLOCK_NONE")
            for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                      "HARM_CATEGORY_SEXUALLY_EXPLICIT", "HARM_CATEGORY_DANGEROUS_CONTENT")
        ],
    )
    resp = await client.aio.models.generate_content(
        model=MODEL,
        contents=build_user_prompt(notes, hours, battery),
        config=config,
    )
    text = (resp.text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    data = json.loads(text)
    return data.get("interpretations") if isinstance(data, dict) else data


async def interpret(notes: Sequence[str], hours: Sequence[Dict[str, Any]],
                    battery: Dict[str, float]) -> List[Dict[str, Any]]:
    """Interpret every operator note. Never raises."""
    raw: Any = None
    try:
        raw = await asyncio.wait_for(_call_gemini(notes, hours, battery), TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("gemini timed out after %ss; falling back to no_op", TIMEOUT_S)
    except Exception:  # noqa: BLE001
        log.exception("gemini interpretation failed; falling back to no_op")
    return sanitize(raw, notes, battery)
