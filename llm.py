"""Operator-note interpretation: Gemini call + deterministic guardrails.

The language model is the interpreter and is always on the path -- the Problem
Statement makes that mandatory and the Participant Guide disqualifies solutions
where an LLM only writes ``plan_summary``. Everything the model returns is
treated as untrusted structured data and must survive :func:`sanitize` before
the optimizer is allowed to see it.

Tuned for a FREE-TIER API key:

  * a key pool (``GEMINI_API_KEYS``) rotated on 429 / quota errors;
  * a model fallback chain, so a per-model daily cap does not end the round;
  * bounded concurrency + retry with jittered backoff, to stay under the
    free-tier requests-per-minute ceiling instead of hammering through it;
  * a normalized note cache, which is the single biggest quota saver -- repeated
    and near-repeated notes across the hidden set never reach the provider, and
    the same note always yields the same answer;
  * a deterministic cross-check (:mod:`rules`) that catches disagreement and
    carries the service through a provider outage rather than emitting no_op for
    every note.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple

import rules

log = logging.getLogger("gridwise.llm")

ALLOWED = {
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
}


# ------------------------------------------------------------------- settings


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# Free-tier daily caps are per model, so keep a couple of alternates ready.
FALLBACK_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_FALLBACK_MODELS", "gemini-2.5-flash-lite,gemini-2.0-flash").split(",")
    if m.strip()]
MODEL_CHAIN: List[str] = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]

TIMEOUT_S = _env_float("LLM_TIMEOUT_S", 12.0)
MAX_ATTEMPTS = _env_int("LLM_MAX_ATTEMPTS", 3)
MAX_CONCURRENCY = _env_int("LLM_MAX_CONCURRENCY", 4)     # free tier is ~10-15 RPM
CACHE_SIZE = _env_int("LLM_CACHE_SIZE", 20000)
ARBITER_ENABLED = os.getenv("LLM_ARBITER", "1").strip().lower() not in ("0", "false", "no")
BREAKER_THRESHOLD = _env_int("LLM_BREAKER_THRESHOLD", 4)
BREAKER_COOLDOWN_S = _env_float("LLM_BREAKER_COOLDOWN_S", 30.0)

_SEMAPHORE: Optional[asyncio.Semaphore] = None


def _semaphore() -> asyncio.Semaphore:
    global _SEMAPHORE
    if _SEMAPHORE is None:
        _SEMAPHORE = asyncio.Semaphore(max(1, MAX_CONCURRENCY))
    return _SEMAPHORE


# -------------------------------------------------------------- client pooling

_clients: Dict[str, Any] = {}
_keys: Optional[List[str]] = None
_key_cursor = 0
_import_failed = False


def _api_keys() -> List[str]:
    global _keys
    if _keys is not None:
        return _keys
    raw = (os.getenv("GEMINI_API_KEYS") or os.getenv("GEMINI_API_KEY")
           or os.getenv("GOOGLE_API_KEY") or "")
    _keys = [k.strip() for k in raw.split(",") if k.strip()]
    if not _keys:
        log.warning("no Gemini API key configured - interpretation will use the "
                    "deterministic fallback path")
    return _keys


def _client_for(key: str):
    global _import_failed
    if key in _clients:
        return _clients[key]
    if _import_failed:
        return None
    try:
        from google import genai
        client = genai.Client(api_key=key)
    except Exception:  # noqa: BLE001
        log.error("could not construct the Gemini client")
        _import_failed = True
        return None
    _clients[key] = client
    return client


def _next_client() -> Optional[Any]:
    """Round-robin the key pool so one free-tier quota is not the ceiling."""
    global _key_cursor
    keys = _api_keys()
    if not keys:
        return None
    key = keys[_key_cursor % len(keys)]
    _key_cursor += 1
    return _client_for(key)


def llm_available() -> bool:
    return bool(_api_keys()) and not _import_failed


def reset_for_tests() -> None:
    """Drop cached keys/clients/cache so tests can re-read the environment."""
    global _keys, _clients, _key_cursor, _import_failed
    _keys = None
    _clients = {}
    _key_cursor = 0
    _import_failed = False
    _cache.clear()
    _breaker.record_success()


# ---------------------------------------------------------------- circuit breaker


class _Breaker:
    """Stops hammering a provider that is down or out of quota."""

    def __init__(self) -> None:
        self.failures = 0
        self.opened_at = 0.0

    @property
    def open(self) -> bool:
        if self.failures < BREAKER_THRESHOLD:
            return False
        if time.monotonic() - self.opened_at >= BREAKER_COOLDOWN_S:
            self.failures = BREAKER_THRESHOLD - 1   # half-open: let one probe through
            return False
        return True

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= BREAKER_THRESHOLD:
            self.opened_at = time.monotonic()

    def record_success(self) -> None:
        self.failures = 0


_breaker = _Breaker()


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
  "01:00 to 04:00"          -> [1, 2, 3]        (a colon means 24-hour clock)
  "10 PM until 2 AM"        -> [0, 1, 22, 23]   (wraps midnight; still ascending)
  "during the 7 PM hour"    -> [19]             (a single hour)
  "throughout the day"      -> [0,1,2,...,23]   (all 24 hours)
  "until midnight" means the end hour is 24, so the last listed hour is 23.
hours must be unique integers 0-23 sorted in ASCENDING order.

RULE 2 - factor IS THE FRACTION THAT REMAINS USABLE, NOT THE REDUCTION.
  "an 80% reduction in solar"          -> factor 0.2
  "solar drops to about 25%"           -> factor 0.25
  "about half the forecast output"     -> factor 0.5
  "roughly one-fifth of normal output" -> factor 0.2
  "output cut by two thirds"           -> factor 0.33
  "derated by 75 percent"              -> factor 0.25
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
Note: "Battery state of charge must not fall below 75 kWh from 8 PM until 11 PM."
  -> minimum_battery_reserve, applies true, {"hours": [20,21,22], "minimum_energy_kwh": 75}
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
                    "directive_type": {"type": "STRING", "enum": sorted(ALLOWED)},
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
    lines = ["OPERATOR NOTES"]
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


def _no_op(idx: int,
           why: str = "This note does not affect today's 24-hour energy schedule."
           ) -> Dict[str, Any]:
    return {
        "note_index": idx,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": why,
    }


def sanitize(raw: Any, notes: Sequence[str],
             battery: Dict[str, float]) -> List[Dict[str, Any]]:
    """Force model output into exactly one valid entry per note, in order.

    Implements the Problem Statement 08 guardrail table: allowed types only, one
    entry per note index with no gaps or duplicates, unique ascending hours
    inside 0-23, ``factor`` clamped to [0, 1], reserve finite / non-negative /
    not above capacity, grid cap finite and non-negative, and ``applies``
    semantics forced consistent. Anything that cannot be validated is demoted to
    ``no_op`` rather than dropped or invented.
    """
    n = len(notes)
    capacity = float(battery.get("capacity_kwh") or 0.0)
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
        explanation = (explanation.strip()
                       if isinstance(explanation, str) and explanation.strip()
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
            v = max(0.0, v)
            clean["minimum_energy_kwh"] = min(capacity, v) if capacity > 0 else v
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


# --------------------------------------------------------------------- note cache

_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_PUNCT = re.compile(r"[^a-z0-9%:. ]+")


def cache_key(note: str, battery: Dict[str, float]) -> str:
    """Normalized note text plus capacity (percentage reserves depend on it)."""
    text = _PUNCT.sub(" ", str(note).lower())
    text = " ".join(text.split())
    return f"{text}|{float(battery.get('capacity_kwh') or 0.0):g}"


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    entry = _cache.get(key)
    if entry is not None:
        _cache.move_to_end(key)
    return entry


def _cache_put(key: str, entry: Dict[str, Any]) -> None:
    _cache[key] = {k: v for k, v in entry.items() if k != "note_index"}
    _cache.move_to_end(key)
    while len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


def cache_stats() -> Dict[str, int]:
    return {"notes_cached": len(_cache)}


# --------------------------------------------------------------------- the LLM call

_QUOTA_MARKERS = ("429", "resource_exhausted", "resource exhausted", "quota",
                  "rate limit", "ratelimit", "too many requests")


def _is_quota_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return any(m in text for m in _QUOTA_MARKERS)


def _parse_payload(text: str) -> Any:
    """Defensive JSON parse -- structured output should not need the fallbacks."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    if isinstance(data, dict):
        return data.get("interpretations", data.get("choices"))
    return data


async def _generate(model: str, client: Any, prompt: str,
                    schema: Dict[str, Any], system: str) -> Any:
    from google.genai import types

    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=0.0,
        response_mime_type="application/json",
        response_schema=schema,
        safety_settings=[
            types.SafetySetting(category=c, threshold="BLOCK_NONE")
            for c in ("HARM_CATEGORY_HARASSMENT", "HARM_CATEGORY_HATE_SPEECH",
                      "HARM_CATEGORY_SEXUALLY_EXPLICIT",
                      "HARM_CATEGORY_DANGEROUS_CONTENT")
        ],
    )
    # Thinking off on the extraction call: it costs seconds of latency and
    # free-tier tokens for a task that does not need it.
    try:
        config.thinking_config = types.ThinkingConfig(thinking_budget=0)
    except Exception:  # noqa: BLE001
        pass

    resp = await client.aio.models.generate_content(
        model=model, contents=prompt, config=config)
    return _parse_payload(getattr(resp, "text", "") or "")


async def _call_model(prompt: str, schema: Dict[str, Any] = RESPONSE_SCHEMA,
                      system: str = SYSTEM_INSTRUCTION) -> Any:
    """One guarded generation, walking the key pool and the model chain."""
    if not llm_available() or _breaker.open:
        return None

    attempts = max(1, MAX_ATTEMPTS)
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        model = MODEL_CHAIN[min(attempt, len(MODEL_CHAIN) - 1)]
        client = _next_client()
        if client is None:
            return None
        try:
            async with _semaphore():
                payload = await asyncio.wait_for(
                    _generate(model, client, prompt, schema, system), TIMEOUT_S)
            if payload is not None:
                _breaker.record_success()
                return payload
            last_exc = ValueError("empty model response")
        except asyncio.TimeoutError as exc:
            last_exc = exc
            log.warning("model call timed out after %ss (model=%s)", TIMEOUT_S, model)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if _is_quota_error(exc):
                # Free-tier RPM/RPD hit: rotate key and drop to the next model.
                log.warning("quota/rate limit on %s; rotating key and model", model)
            else:
                log.warning("model call failed on %s: %s", model, type(exc).__name__)
        if attempt < attempts - 1:
            await asyncio.sleep(min(2.0, 0.25 * (2 ** attempt)) * (0.5 + random.random()))

    _breaker.record_failure()
    if last_exc is not None:
        log.warning("interpretation call exhausted retries: %s", type(last_exc).__name__)
    return None


# ------------------------------------------------------------------ cross-checking


def _same_reading(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a.get("directive_type") != b.get("directive_type"):
        return False
    aa, bb = a.get("structured_adjustment"), b.get("structured_adjustment")
    if aa is None or bb is None:
        return aa is None and bb is None
    if list(aa.get("hours", [])) != list(bb.get("hours", [])):
        return False
    for key in ("factor", "minimum_energy_kwh", "max_grid_kwh"):
        if (key in aa) != (key in bb):
            return False
        if key in aa and abs(float(aa[key]) - float(bb[key])) > 0.01:
            return False
    return True


_ARBITER_SCHEMA: Dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "choices": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "note_index": {"type": "INTEGER"},
                    "choice": {"type": "STRING", "enum": ["A", "B"]},
                },
                "required": ["note_index", "choice"],
            },
        }
    },
    "required": ["choices"],
}

_ARBITER_SYSTEM = """You are settling disagreements between two candidate readings
of a campus operator note. Apply these rules exactly:

  * Time windows are START-INCLUSIVE and END-EXCLUSIVE: "1 PM to 3 PM" -> [13,14].
  * A colon means a 24-hour clock: "01:00 to 04:00" -> [1,2,3].
  * For solar_reduction, `factor` is the fraction of solar that REMAINS usable:
    an 80% reduction means factor 0.2.
  * A note that does not change today's electricity schedule is no_op.
  * Percentages of battery capacity must be resolved into absolute kWh.

For each disagreement choose the candidate that follows these rules. Answer with
"A" or "B" only."""


async def _arbitrate(disputes: List[Tuple[int, str, Dict[str, Any], Dict[str, Any]]],
                     battery: Dict[str, float]) -> Dict[int, str]:
    """One bounded extra call to settle type/value disagreements."""
    if not disputes or not ARBITER_ENABLED:
        return {}
    lines = [f"Battery capacity is {battery.get('capacity_kwh')} kWh.", ""]
    for idx, note, a, b in disputes:
        lines.append(f"NOTE {idx}: {note}")
        lines.append(f"  A: {a.get('directive_type')} "
                     f"{json.dumps(a.get('structured_adjustment'))}")
        lines.append(f"  B: {b.get('directive_type')} "
                     f"{json.dumps(b.get('structured_adjustment'))}")
        lines.append("")
    payload = await _call_model("\n".join(lines), _ARBITER_SCHEMA, _ARBITER_SYSTEM)
    out: Dict[int, str] = {}
    if isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("note_index"))
            except (TypeError, ValueError):
                continue
            choice = str(item.get("choice", "")).strip().upper()
            if choice in ("A", "B"):
                out[idx] = choice
    return out


# ------------------------------------------------------------------- entry point

#: Rule readings at or above this confidence are trusted when the model returned
#: no_op and the arbiter could not be reached. A missed directive costs both the
#: interpretation mark and the downstream-application mark for that case.
_RULE_TRUST = 0.7


async def interpret(notes: Sequence[str], hours: Sequence[Dict[str, Any]],
                    battery: Dict[str, float]) -> List[Dict[str, Any]]:
    """Interpret every operator note. Never raises.

    Returns exactly ``len(notes)`` guard-railed entries indexed 0..N-1.
    """
    n = len(notes)
    rule_reading = rules.extract(notes, battery)
    used_fallback = False

    # 1. cache lookup -- repeated notes never reach the provider
    final: List[Optional[Dict[str, Any]]] = [None] * n
    pending: List[int] = []
    for i, note in enumerate(notes):
        hit = _cache_get(cache_key(note, battery))
        if hit is not None:
            final[i] = dict(hit, note_index=i)
        else:
            pending.append(i)

    if pending:
        # 2. the model call -- one round trip for every uncached note
        sub_notes = [notes[i] for i in pending]
        raw = await _call_model(build_user_prompt(sub_notes, hours, battery))
        model_reading = sanitize(raw, sub_notes, battery)
        used_fallback = raw is None
        if used_fallback:
            log.warning("model unavailable; using the deterministic reading for "
                        "%d note(s)", len(sub_notes))

        # 3. cross-check the model against the deterministic reading
        disputes: List[Tuple[int, str, Dict[str, Any], Dict[str, Any]]] = []
        if not used_fallback:
            for local, i in enumerate(pending):
                m, r = model_reading[local], rule_reading[i]
                if _same_reading(m, r):
                    continue
                if (r.get("directive_type") == "no_op"
                        and float(r.get("confidence", 0.0)) < _RULE_TRUST):
                    continue    # rules just did not parse the note; trust the model
                disputes.append((i, notes[i], m, r))

        verdicts = await _arbitrate(disputes, battery) if disputes else {}

        for local, i in enumerate(pending):
            if used_fallback:
                final[i] = dict(rule_reading[i])
                continue
            chosen = model_reading[local]
            dispute = next((d for d in disputes if d[0] == i), None)
            if dispute is not None:
                verdict = verdicts.get(i)
                if verdict == "B":
                    chosen = dispute[3]
                elif (verdict is None
                      and chosen.get("directive_type") == "no_op"
                      and float(rule_reading[i].get("confidence", 0.0)) >= _RULE_TRUST):
                    # No arbiter available and the model saw nothing where the
                    # deterministic reading is confident: take the directive.
                    chosen = dispute[3]
                if chosen is not model_reading[local]:
                    log.info("cross-check overrode note %s: %s -> %s", i,
                             model_reading[local].get("directive_type"),
                             chosen.get("directive_type"))
            final[i] = dict(chosen)

    # 4. re-run the guardrails over the merged result, then cache it
    for i, entry in enumerate(final):
        if entry is not None:
            entry.pop("confidence", None)
            entry["note_index"] = i
    merged = sanitize([e for e in final if e is not None], notes, battery)
    if pending and not used_fallback:
        # Only a real model answer is cached. A rule reading taken during an
        # outage must not stick once the provider comes back.
        for i in pending:
            _cache_put(cache_key(notes[i], battery), merged[i])
    return merged
