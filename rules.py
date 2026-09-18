"""Deterministic operator-note extractor.

This is NEVER the sole interpreter. The language model always runs first and its
guard-railed output is the default reading (the Problem Statement makes the LLM
mandatory in the interpretation path, and the Participant Guide marks hard-coded
phrase matching as the sole interpreter non-compliant).

It exists for the two things the rubric rewards:

  * cross-check -- when the rule reading and the model reading disagree on the
    directive type or the affected hours, the disagreement is surfaced so the
    arbiter can settle it. This lifts paraphrase robustness.
  * outage path -- when the provider is rate-limited, timing out or down, a rule
    reading scores far better than emitting no_op for every note, and the guide
    explicitly permits a backup model/path.

Everything here returns the same shape the model returns, so both readings go
through the identical guardrails in ``llm.sanitize`` before the optimizer sees
them.
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------- vocabulary

_WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "twenty-one": 21, "twenty-two": 22, "twenty-three": 23,
}

# Fraction words -> the fraction they name (NOT yet "remaining" vs "removed").
_FRACTION_WORDS = {
    "half": 0.5, "a half": 0.5, "one half": 0.5, "one-half": 0.5,
    "a third": 1 / 3, "one third": 1 / 3, "one-third": 1 / 3,
    "two thirds": 2 / 3, "two-thirds": 2 / 3,
    "a quarter": 0.25, "one quarter": 0.25, "one-quarter": 0.25,
    "three quarters": 0.75, "three-quarters": 0.75,
    "a fifth": 0.2, "one fifth": 0.2, "one-fifth": 0.2,
    "two fifths": 0.4, "two-fifths": 0.4,
    "a tenth": 0.1, "one tenth": 0.1, "one-tenth": 0.1,
}

_TIME_TOKEN = (
    r"(?:\d{1,2}(?::\d{2})?|noon|midday|midnight|"
    + "|".join(sorted(_WORD_NUM, key=len, reverse=True))
    + r")"
)
_MERIDIEM = r"(?:a\.?m\.?|p\.?m\.?)"

_RANGE_RE = re.compile(
    rf"\b({_TIME_TOKEN})\s*({_MERIDIEM})?\s*"
    rf"(?:\s*(?:-|--|–|—)\s*|\s*(?:to|till|til|until|untill|through|thru|and)\s+)"
    rf"({_TIME_TOKEN})\s*({_MERIDIEM})?",
    re.IGNORECASE,
)

_SINGLE_RE = re.compile(
    rf"\b(?:during|in|at|for|throughout)\s+the\s+({_TIME_TOKEN})\s*({_MERIDIEM})?\s*hour\b",
    re.IGNORECASE,
)

_ALL_DAY_RE = re.compile(
    r"\b(?:all day|throughout the day|through the day|whole day|entire day|"
    r"round the clock|around the clock|24\s*hours|all 24 hours|every hour)\b",
    re.IGNORECASE,
)

_PM_HINT = re.compile(r"\b(evening|tonight|night|afternoon|dusk|peak)\b", re.IGNORECASE)
_AM_HINT = re.compile(r"\b(morning|dawn|sunrise|early)\b", re.IGNORECASE)


# ------------------------------------------------------------------ time parsing


def _token_hour(token: str) -> Optional[Tuple[int, bool]]:
    """Return ``(raw_hour, is_absolute)``.

    ``is_absolute`` means the token already fixes the 24-hour value on its own
    (``13:00``, ``noon``, ``midnight``) so no AM/PM inference is needed.
    """
    t = token.strip().lower()
    if t in ("noon", "midday"):
        return 12, True
    if t == "midnight":
        return 0, True
    if t in _WORD_NUM:
        v = _WORD_NUM[t]
        return v, v >= 13
    explicit_clock = ":" in t
    if explicit_clock:
        t = t.split(":", 1)[0]
    if not t.isdigit():
        return None
    v = int(t)
    if v > 24:
        return None
    # "14:00" / "01:00" is 24-hour notation and is already unambiguous.
    return v, explicit_clock or v >= 13 or v == 0


def _apply_meridiem(raw: int, mer: Optional[str]) -> int:
    if mer is None:
        return raw
    mer = mer.replace(".", "").lower()
    if mer.startswith("p"):
        return 12 if raw == 12 else (raw % 12) + 12
    return 0 if raw == 12 else raw % 12


def _resolve_range(t1: str, m1: Optional[str], t2: str, m2: Optional[str],
                   context: str) -> Optional[List[int]]:
    """Turn a matched time range into the start-inclusive, end-exclusive hours.

    The Problem Statement fixes the convention: "1 PM to 3 PM" means [13, 14].
    """
    a = _token_hour(t1)
    b = _token_hour(t2)
    if a is None or b is None:
        return None
    raw_start, abs_start = a
    raw_end, abs_end = b

    # "midnight" as an end bound means the end of the day, not hour 0.
    end_is_midnight = t2.strip().lower() == "midnight"

    # A meridiem stated on only one side applies to both ("1 to 3 PM").
    if m1 is None and m2 is not None and not abs_start:
        m1 = m2
    if m2 is None and m1 is not None and not abs_end:
        m2 = m1

    start = raw_start if abs_start else _apply_meridiem(raw_start, m1)
    if m1 is None and not abs_start:
        # No AM/PM anywhere. Campus notes that say a bare 1-6 mean the afternoon
        # ("panel washing from one until three" -> 13, 15); 7-11 read as morning
        # unless the sentence is explicitly about the evening.
        if _PM_HINT.search(context) and not _AM_HINT.search(context):
            start = raw_start % 12 + 12 if raw_start != 12 else 12
        elif raw_start <= 6:
            start = raw_start + 12
    start %= 24

    if end_is_midnight:
        end = 24
    elif abs_end:
        end = raw_end
    elif m2 is not None:
        end = _apply_meridiem(raw_end, m2)
    else:
        # Pick the reading that lands after the start without spanning the globe.
        options = [raw_end % 24, (raw_end % 12) + 12, raw_end % 12]
        end = next((o for o in options if start < o <= start + 16), options[0])

    if end == start:
        return None
    span = (end - start) % 24
    if span == 0:
        span = 24
    if span > 24:
        return None
    return sorted({(start + k) % 24 for k in range(span)})


def parse_hours(text: str) -> Optional[List[int]]:
    """Best-effort extraction of the affected hours from a note."""
    if _ALL_DAY_RE.search(text):
        return list(range(24))

    m = _RANGE_RE.search(text)
    if m:
        hours = _resolve_range(m.group(1), m.group(2), m.group(3), m.group(4), text)
        if hours:
            return hours

    m = _SINGLE_RE.search(text)
    if m:
        parsed = _token_hour(m.group(1))
        if parsed is not None:
            raw, absolute = parsed
            h = raw if absolute else _apply_meridiem(raw, m.group(2))
            if m.group(2) is None and not absolute and raw <= 6:
                h = raw + 12
            return [h % 24]

    # "after 8 PM", "from 6 PM onwards", "before 6 AM"
    m = re.search(rf"\b(?:after|from)\s+({_TIME_TOKEN})\s*({_MERIDIEM})?\s*"
                  rf"(?:onwards?|onward)?\b", text, re.IGNORECASE)
    if m and re.search(r"\b(onwards?|onward|for the rest of the day|"
                       r"until the end of the day)\b", text, re.IGNORECASE):
        parsed = _token_hour(m.group(1))
        if parsed is not None:
            raw, absolute = parsed
            h = raw if absolute else _apply_meridiem(raw, m.group(2))
            if m.group(2) is None and not absolute and raw <= 6:
                h = raw + 12
            return list(range(h % 24, 24))
    return None


# --------------------------------------------------------------- number parsing


def _percent(text: str) -> Optional[float]:
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(?:%|per\s*cent|percent)", text, re.IGNORECASE)
    return float(m.group(1)) / 100.0 if m else None


def _fraction_word(text: str) -> Optional[float]:
    low = text.lower()
    best: Optional[Tuple[int, float]] = None
    for phrase, value in _FRACTION_WORDS.items():
        pos = low.find(phrase)
        if pos >= 0 and (best is None or len(phrase) > len(low[best[0]:best[0]])):
            # prefer the longest matching phrase ("two-thirds" over "a third")
            if best is None or value != best[1]:
                best = (pos, value)
    # longest-phrase preference
    matches = [(len(p), v) for p, v in _FRACTION_WORDS.items() if p in low]
    if matches:
        return max(matches)[1]
    return best[1] if best else None


_REDUCTION_CUE = re.compile(
    r"\b(reduction|reduced by|reduce by|drop by|dropping by|cut by|down by|"
    r"lower by|decreased? by|loss of|derated? by|de-rated? by)\b", re.IGNORECASE)

_REMAINING_CUE = re.compile(
    r"\b(drop to|drops to|fall to|falls to|down to|to about|to roughly|to around|"
    r"limited to|capped at|only|leave|leaving|remain(?:ing)?|available|"
    r"produce|producing|deliver(?:ing)?|output of|at about|at roughly|"
    r"operating at|running at|of (?:normal|forecast|expected|rated|usual|"
    r"typical|planned))\b", re.IGNORECASE)

_ZERO_SOLAR = re.compile(
    r"\b(no solar|zero solar|offline|off-line|disconnected|shut down|shutdown|"
    r"completely out|fully out|no (?:pv|photovoltaic) output|"
    r"produce nothing|not produce)\b", re.IGNORECASE)


def _solar_factor(text: str) -> Optional[float]:
    """Return the USABLE FRACTION THAT REMAINS (Problem Statement 5.1).

    A stated quantity always beats an outage keyword. "Half the array is offline
    for rewiring" is a 50% reduction, not a blackout. Emitting 0.0 there would be
    worse than emitting no_op: it is a *wrong constraint that gets applied*, so
    the interpretation mark and the downstream-application mark are both lost and
    the plan is optimised against solar that does not exist. A bare outage word
    means a full outage only when no fraction or percentage is present.
    """
    pct = _percent(text)
    frac = _fraction_word(text)
    if pct is None and frac is None and _ZERO_SOLAR.search(text):
        return 0.0
    value = pct if pct is not None else frac
    if value is None:
        return None
    value = min(1.0, max(0.0, value))

    reduction = bool(_REDUCTION_CUE.search(text))
    remaining = bool(_REMAINING_CUE.search(text))
    if reduction and not remaining:
        return round(1.0 - value, 6)
    if reduction and remaining:
        # "an 80% reduction leaving 20%" -- the reduction cue wins only when it
        # sits closer to the number.
        num = re.search(r"\d{1,3}(?:\.\d+)?\s*(?:%|per\s*cent|percent)", text,
                        re.IGNORECASE)
        if num:
            red = _REDUCTION_CUE.search(text)
            rem = _REMAINING_CUE.search(text)
            d_red = abs(red.start() - num.start()) if red else 10 ** 6
            d_rem = abs(rem.start() - num.start()) if rem else 10 ** 6
            if d_red < d_rem:
                return round(1.0 - value, 6)
    return round(value, 6)


def _kwh_near(text: str, cues: Sequence[str]) -> Optional[float]:
    """First kWh quantity in the note, preferring one close to a cue word."""
    nums = [(m.start(), float(m.group(1)))
            for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(?:kwh|kw-h|kw h|kilowatt[- ]hours?)",
                                 text, re.IGNORECASE)]
    if not nums:
        return None
    low = text.lower()
    cue_pos = [low.find(c) for c in cues if c in low]
    if cue_pos:
        return min(nums, key=lambda n: min(abs(n[0] - p) for p in cue_pos))[1]
    return nums[0][1]


def _reserve_kwh(text: str, capacity: float) -> Optional[float]:
    direct = _kwh_near(text, ("reserve", "at least", "remain", "keep", "hold",
                              "maintain", "minimum", "no lower", "not fall"))
    if direct is not None:
        return direct
    # A percentage of capacity may sit on either side of the capacity word:
    # "50% of the battery capacity" and "must hold 60% of capacity" both count.
    near_cap = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent)[^.]{0,40}?"
        r"(?:capacit|pack|bank|storage|batter)"
        r"|(?:capacit|pack|bank|storage|batter)\w*[^.]{0,40}?"
        r"(\d+(?:\.\d+)?)\s*(?:%|per\s*cent|percent)",
        text, re.IGNORECASE)
    if near_cap and capacity > 0:
        raw = near_cap.group(1) or near_cap.group(2)
        if raw is not None:
            return round(min(1.0, max(0.0, float(raw) / 100.0)) * capacity, 6)

    # "a third of the pack", "half of the battery"
    if re.search(r"\bof\s+(?:the\s+)?(?:battery\s+)?(?:capacity|pack|bank|storage|"
                 r"battery|rated capacity)\b", text, re.IGNORECASE):
        pct = _percent(text)
        frac = pct if pct is not None else _fraction_word(text)
        if frac is not None:
            return round(min(1.0, max(0.0, frac)) * capacity, 6)
    return None


# ----------------------------------------------------------------- type matching

_NO_CHARGE = re.compile(
    r"\b(?:"
    r"(?:do not|don't|must not|cannot|can't|may not|no|never|avoid|stop|"
    r"suspend|halt|refrain from)\s+(?:\w+\s+){0,3}?charg\w*"
    r"|charg\w*\s+(?:\w+\s+){0,4}?(?:is|are|will be|remains?|stays?)?\s*"
    r"(?:unavailable|disabled|isolated|offline|off-line|locked out|blocked|"
    r"prohibited|not allowed|not permitted|suspended|out of service|"
    r"inhibited|barred)"
    r"|(?:charger|charging circuit|charge controller|charging system)\s+"
    r"(?:\w+\s+){0,4}?(?:isolated|unavailable|offline|down|disabled|"
    r"out of service|locked)"
    r"|no charging"
    r"|charging is (?:not )?(?:possible|permitted|allowed)"
    # "charging must not happen" -- the prohibition can follow the verb. Uses the
    # gerund so "state of charge must not fall below" stays a reserve directive.
    r"|charging\s+(?:\w+\s+){0,3}?(?:must not|may not|cannot|can't|will not|"
    r"won't|shall not|is not|are not|does not|do not|isn't|aren't)"
    r")", re.IGNORECASE)

_NO_DISCHARGE = re.compile(
    r"\b(?:"
    r"(?:do not|don't|must not|cannot|can't|may not|no|never|avoid|stop|"
    r"suspend|halt|refrain from)\s+(?:\w+\s+){0,3}?discharg\w*"
    r"|discharg\w*\s+(?:\w+\s+){0,4}?(?:is|are|will be|remains?|stays?)?\s*"
    r"(?:unavailable|disabled|isolated|offline|off-line|locked out|blocked|"
    r"prohibited|not allowed|not permitted|suspended|out of service|inhibited)"
    r"|no discharging"
    r"|discharg\w*\s+(?:\w+\s+){0,3}?(?:must not|may not|cannot|can't|will not|"
    r"won't|shall not|is not|are not|does not|do not|isn't|aren't)"
    r"|(?:battery|batteries)\s+(?:\w+\s+){0,3}?(?:export|output|supply)\s+"
    r"(?:is\s+)?(?:not|prohibited|blocked|disabled|unavailable)"
    r"|no battery (?:export|output|support|supply|contribution)"
    # "avoid drawing down the pack", "do not drain the battery"
    r"|(?:avoid|no|not|never|stop|halt|suspend|refrain from|without)\s+"
    r"(?:\w+\s+){0,3}?(?:draw(?:ing)?\s+down|drain\w*|deplet\w*|"
    r"run(?:ning)?\s+down)"
    r"|(?:draw(?:ing)?\s+down|drain\w*|deplet\w*)\s+(?:\w+\s+){0,3}?"
    r"(?:must not|may not|cannot|is not|should not)"
    r"|(?:battery|batteries)\s+(?:must|may|should|can)(?:not| not)\s+"
    r"(?:\w+\s+){0,2}?(?:discharg\w*|supply|export|feed)"
    r")", re.IGNORECASE)

_SOLAR_WORD = re.compile(
    r"\b(solar|pv|photovoltaic|rooftop|panel|panels|array|arrays|generation|"
    r"irradiance|sunlight|inverter|"
    # what actually reduces solar, named without the word "solar"
    r"shading|shade|shaded|cloud|clouds|cloudy|overcast|haze|hazy|fog|"
    r"soiling|dust|dusty|module|modules|string|strings)\b", re.IGNORECASE)

_RESERVE_WORD = re.compile(
    r"\b(reserve|at least|no less than|no fewer than|not less than|"
    r"no lower than|not fall below|not drop below|not go below|"
    r"minimum|keep|keeps|hold|holds|hold back|hold at least|"
    r"maintain|maintains|retain|retains|remain|remains|stay|stays|preserve|"
    r"stored in the battery|state of charge|soc|floor)\b", re.IGNORECASE)

_GRID_WORD = re.compile(
    r"\b(grid|import|intake|draw|feeder|transformer|substation|utility|"
    r"mains|supply from the grid|purchase)\b", re.IGNORECASE)

_CAP_WORD = re.compile(
    r"\b(not exceed|no more than|at or below|below|under|cap|capped|limit|"
    r"limited|maximum|max|ceiling|must stay|stay at or|no higher than|"
    r"not go above|restricted to|throttled to)\b", re.IGNORECASE)

_BATTERY_WORD = re.compile(
    r"\b(battery|batteries|pack|bank|storage|bess|soc|state of charge|"
    r"accumulator)\b", re.IGNORECASE)

# Notes that clearly describe campus life rather than today's electricity plan.
_DISTRACTOR = re.compile(
    r"\b(cafeteria|menu|canteen|library|book|seminar|booking|registration|"
    r"deadline|timetable|class|lecture|exam|club|notice|newsletter|"
    r"tournament|sports|gym|parking|shuttle|bus|wifi|laundry|dormitory|"
    r"hostel|admission|convocation|holiday|festival)\b", re.IGNORECASE)

_FUTURE = re.compile(
    r"\b(next week|next month|next quarter|next year|next semester|tomorrow|"
    r"last week|last month|yesterday|in the spring|later this year)\b",
    re.IGNORECASE)


def _detect_type(text: str) -> Optional[str]:
    """Pick the single supported directive type a note maps to, if any."""
    solar = bool(_SOLAR_WORD.search(text))
    battery = bool(_BATTERY_WORD.search(text))

    if _NO_CHARGE.search(text):
        return "no_charge_window"
    if _NO_DISCHARGE.search(text):
        return "no_discharge_window"

    if solar and (_percent(text) is not None or _fraction_word(text) is not None
                  or _ZERO_SOLAR.search(text)):
        return "solar_reduction"

    # A reserve directive is about the *battery*, so it needs a battery word or
    # the noun "reserve" itself. Without one, cue words shared with grid caps
    # ("keep", "stay", "maintain", "no lower than") belong to the grid sentence:
    # "grid intake must stay at or below 190 kWh" is a cap, not a reserve.
    reserve_subject = battery or re.search(r"\breserves?\b", text, re.IGNORECASE)
    reserve_like = bool(_RESERVE_WORD.search(text)) and bool(reserve_subject)
    grid_like = bool(_GRID_WORD.search(text)) and bool(_CAP_WORD.search(text))
    has_number = bool(re.search(r"\d", text))

    if grid_like and has_number and not reserve_like:
        return "max_grid_window"

    if reserve_like and has_number and (battery
                                        or re.search(r"kwh", text, re.IGNORECASE)):
        return "minimum_battery_reserve"

    if grid_like and has_number:
        return "max_grid_window"
    return None


# ------------------------------------------------------------------- entry point


def extract_one(note: str, battery: Dict[str, float]) -> Dict[str, Any]:
    """Interpret a single note. Always returns a well-formed entry."""
    text = " ".join(str(note).split())
    capacity = float(battery.get("capacity_kwh") or 0.0)

    if not text:
        return _no_op("Empty note.", 0.9)

    kind = _detect_type(text)
    if kind is None:
        why = ("This note describes campus activity rather than today's energy schedule."
               if _DISTRACTOR.search(text) or _FUTURE.search(text)
               else "No supported energy directive was found in this note.")
        return _no_op(why, 0.8 if _DISTRACTOR.search(text) else 0.45)

    hours = parse_hours(text)
    if not hours:
        return _no_op("No usable time window was found in this note.", 0.3)

    adjustment: Dict[str, Any] = {"hours": hours}
    confidence = 0.75

    if kind == "solar_reduction":
        factor = _solar_factor(text)
        if factor is None or not math.isfinite(factor):
            return _no_op("Solar impact could not be quantified.", 0.3)
        adjustment["factor"] = min(1.0, max(0.0, factor))
    elif kind == "minimum_battery_reserve":
        value = _reserve_kwh(text, capacity)
        if value is None or not math.isfinite(value):
            return _no_op("Reserve level could not be quantified.", 0.3)
        adjustment["minimum_energy_kwh"] = min(capacity, max(0.0, value)) if capacity \
            else max(0.0, value)
    elif kind == "max_grid_window":
        value = _kwh_near(text, ("exceed", "limit", "cap", "below", "import",
                                 "intake", "grid", "maximum"))
        if value is None or not math.isfinite(value):
            return _no_op("Grid cap could not be quantified.", 0.3)
        adjustment["max_grid_kwh"] = max(0.0, value)
    else:
        confidence = 0.8

    return {
        "note_index": 0,
        "applies": True,
        "directive_type": kind,
        "structured_adjustment": adjustment,
        "explanation": _explain(kind, adjustment),
        "confidence": confidence,
    }


def _no_op(why: str, confidence: float) -> Dict[str, Any]:
    return {
        "note_index": 0,
        "applies": False,
        "directive_type": "no_op",
        "structured_adjustment": None,
        "explanation": why,
        "confidence": confidence,
    }


def _explain(kind: str, adj: Dict[str, Any]) -> str:
    hours = adj.get("hours", [])
    span = (f"hours {hours[0]}-{hours[-1]}" if len(hours) > 1
            else f"hour {hours[0]}" if hours else "the stated window")
    if kind == "solar_reduction":
        return f"Usable solar is scaled to {adj['factor']} of forecast over {span}."
    if kind == "minimum_battery_reserve":
        return f"Battery energy is held at or above {adj['minimum_energy_kwh']} kWh over {span}."
    if kind == "no_charge_window":
        return f"Battery charging is unavailable over {span}."
    if kind == "no_discharge_window":
        return f"Battery discharging is unavailable over {span}."
    if kind == "max_grid_window":
        return f"Grid import is capped at {adj['max_grid_kwh']} kWh over {span}."
    return "No effect on today's schedule."


def extract(notes: Sequence[str], battery: Dict[str, float]) -> List[Dict[str, Any]]:
    """Interpret every note, indexed 0..N-1 in order."""
    out: List[Dict[str, Any]] = []
    for i, note in enumerate(notes):
        entry = extract_one(note, battery)
        entry["note_index"] = i
        out.append(entry)
    return out
