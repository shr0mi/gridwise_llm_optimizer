"""GridWise LLM service - BUP CSE Fest 2026 online preliminary.

GET  /health           readiness probe
POST /optimize-energy  LLM note interpretation + 24-hour cost-optimal schedule

Pipeline:  request guard -> LLM interpretation -> deterministic guardrails ->
           LP optimizer -> judge-equivalent replay -> response.

Interactive API documentation (Swagger UI) is served at /docs, ReDoc at /redoc
and the raw OpenAPI schema at /openapi.json.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse

import config  # noqa: F401  -- loads .env before any setting is read
import llm
import optimizer
import replay
from schemas import (ErrorResponse, HealthResponse, OptimizeResponse,
                     ScenarioRequest, SemanticError)

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("gridwise")

REQUEST_BUDGET_S = float(os.getenv("REQUEST_BUDGET_S", "25"))   # judge limit is 30s
INTERPRET_BUDGET_S = REQUEST_BUDGET_S * 0.6
SCENARIO_CACHE_SIZE = int(float(os.getenv("SCENARIO_CACHE_SIZE", "2000")))
SCENARIO_CACHE_TTL_S = float(os.getenv("SCENARIO_CACHE_TTL_S", "3600"))

DESCRIPTION = """
LLM-assisted smart-campus energy scheduling for the BUP CSE Fest 2026 preliminary.

`POST /optimize-energy` takes a 24-hour scenario plus 1-3 natural-language
operator notes. A language model converts every note into a structured directive,
deterministic guardrails validate that output, an exact linear program builds the
cheapest 24-hour grid / solar / battery schedule that satisfies the GridWise rules
and every applicable directive, and the finished plan is replayed hour by hour
before it is returned.

**Supported directive types**

| type | structured_adjustment |
|---|---|
| `solar_reduction` | `{"hours": [...], "factor": 0..1}` |
| `minimum_battery_reserve` | `{"hours": [...], "minimum_energy_kwh": number}` |
| `no_charge_window` | `{"hours": [...]}` |
| `no_discharge_window` | `{"hours": [...]}` |
| `max_grid_window` | `{"hours": [...], "max_grid_kwh": number}` |
| `no_op` | `null` |

Time windows are start-inclusive and end-exclusive: 1 PM to 3 PM is `[13, 14]`.
For `solar_reduction`, `factor` is the fraction of solar that remains usable.
"""

app = FastAPI(
    title="GridWise LLM",
    version="1.1.0",
    description=DESCRIPTION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={"name": "GridWise LLM service"},
    openapi_tags=[
        {"name": "health", "description": "Readiness probe used by the judge harness."},
        {"name": "optimize", "description": "Operator-note interpretation and "
                                            "24-hour schedule optimization."},
    ],
)


# ------------------------------------------------------------------ error handling


def _is_semantic(exc: RequestValidationError) -> bool:
    """True when the body parsed fine but its numbers contradict each other."""
    for err in exc.errors():
        cause = (err.get("ctx") or {}).get("error")
        if isinstance(cause, SemanticError):
            return True
        if err.get("type") == "value_error" and "SemanticError" in str(cause):
            return True
    return False


def _safe_detail(exc: RequestValidationError) -> List[Dict[str, Any]]:
    """Validation detail with the raw input stripped out -- it can be anything."""
    out: List[Dict[str, Any]] = []
    for err in exc.errors()[:10]:
        out.append({
            "loc": [str(p) for p in err.get("loc", [])],
            "msg": str(err.get("msg", ""))[:200],
            "type": str(err.get("type", "")),
        })
    return out


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    """400 for malformed or structurally invalid; 422 for semantic conflicts.

    Problem Statement 6.1: 400 covers "malformed JSON or structurally invalid
    request"; 422 is the optional code for a well-formed but semantically
    invalid one.
    """
    if _is_semantic(exc):
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"error": "semantically invalid request",
                     "detail": _safe_detail(exc)},
        )
    malformed = any(e.get("type") == "json_invalid" for e in exc.errors())
    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content={"error": "malformed request" if malformed else "invalid request",
                 "detail": _safe_detail(exc)},
    )


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception):
    """Controlled 500: no stack trace, no prompt, no key ever reaches the client."""
    log.error('{"event":"unhandled","path":"%s","type":"%s"}',
              request.url.path, type(exc).__name__)
    return JSONResponse(status_code=500, content={"error": "internal error"})


# ------------------------------------------------------------------ scenario cache

_scenario_cache: "OrderedDict[str, tuple]" = OrderedDict()


def _scenario_key(payload: ScenarioRequest) -> str:
    blob = json.dumps(payload.model_dump(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    hit = _scenario_cache.get(key)
    if hit is None:
        return None
    stored_at, body = hit
    if time.monotonic() - stored_at > SCENARIO_CACHE_TTL_S:
        _scenario_cache.pop(key, None)
        return None
    _scenario_cache.move_to_end(key)
    return body


def _cache_put(key: str, body: Dict[str, Any]) -> None:
    _scenario_cache[key] = (time.monotonic(), body)
    _scenario_cache.move_to_end(key)
    while len(_scenario_cache) > SCENARIO_CACHE_SIZE:
        _scenario_cache.popitem(last=False)


# ----------------------------------------------------------------------- endpoints


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get(
    "/health",
    tags=["health"],
    response_model=HealthResponse,
    summary="Readiness probe",
    description="Static readiness response. Touches neither the model nor the "
                "solver, so it stays fast and returns 200 even during a "
                "provider outage.",
    responses={200: {"description": "Service is ready.",
                     "content": {"application/json": {"example": {"status": "ok"}}}}},
)
async def health() -> Dict[str, str]:
    return {"status": "ok"}


def _summarize(directives: List[Dict[str, Any]], result: Dict[str, Any],
               tier: str) -> str:
    applied = [d for d in directives if d.get("applies")]
    parts = []
    for d in applied:
        adj = d.get("structured_adjustment") or {}
        hrs = adj.get("hours") or []
        if len(hrs) > 1:
            span = f"hours {hrs[0]}-{hrs[-1]}"
        elif hrs:
            span = f"hour {hrs[0]}"
        else:
            span = "the stated window"
        parts.append(f"{d['directive_type']} over {span}")
    head = ("Applied " + "; ".join(parts) + ". " if parts
            else "No operator note changed today's schedule. ")

    skipped = len(directives) - len(applied)
    tail = f"{skipped} note(s) were irrelevant and ignored. " if skipped else ""
    if tier.startswith("dropped:"):
        tail += ("Some extracted directives could not be satisfied together and "
                 "were relaxed to keep the plan feasible. ")
    elif tier == "idle_fallback":
        tail += "A conservative battery-idle plan was used to stay feasible. "

    return (head + tail +
            f"Charged the battery in cheap hours and discharged it into the "
            f"evening peak, curtailing surplus solar, for a total grid draw of "
            f"{result['total_grid_kwh']:.2f} kWh costing "
            f"{result['total_cost_bdt']:.2f} BDT with a peak of "
            f"{result['peak_grid_kwh']:.2f} kWh.")


@app.post(
    "/optimize-energy",
    tags=["optimize"],
    response_model=OptimizeResponse,
    response_model_exclude_none=False,
    status_code=200,
    summary="Interpret operator notes and return the optimal 24-hour plan",
    description=(
        "Interprets every operator note with the language model, validates the "
        "result through deterministic guardrails, applies the surviving "
        "directives to an exact linear program, replays the finished schedule "
        "hour by hour, and returns the interpretation together with the plan.\n\n"
        "Returns exactly one `directive_interpretation` entry per note in "
        "`note_index` order, and exactly 24 `hourly_plan` entries."
    ),
    responses={
        200: {"description": "Interpretation and optimized 24-hour schedule."},
        400: {"model": ErrorResponse,
              "description": "Malformed JSON or structurally invalid request."},
        422: {"model": ErrorResponse,
              "description": "Well-formed request with contradictory values."},
        500: {"model": ErrorResponse,
              "description": "Controlled internal error. No secrets or stack traces."},
    },
)
async def optimize_energy(payload: ScenarioRequest) -> JSONResponse:
    key = _scenario_key(payload)
    cached = _cache_get(key)
    if cached is not None:
        return JSONResponse(status_code=200, content=cached)

    hours = [h.model_dump() for h in payload.hours]
    battery = payload.battery.model_dump()
    notes = payload.operator_notes

    # 1. interpretation -- the model is the interpreter; guardrails gate its output
    try:
        directives = await asyncio.wait_for(
            llm.interpret(notes, hours, battery), INTERPRET_BUDGET_S)
    except asyncio.TimeoutError:
        log.warning("interpretation exceeded the request budget; using the "
                    "deterministic reading")
        import rules
        directives = llm.sanitize(rules.extract(notes, battery), notes, battery)

    # 2. optimization -- off the event loop; HiGHS releases the GIL
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, optimizer.optimize, hours, battery, directives)
    tier = result.pop("tier", "optimal")

    # 3. final replay -- an invalid plan is never emitted (Problem Statement 08)
    errs = replay.validate(hours, battery, directives, result)
    if errs:
        log.warning('{"event":"replay_rejected","scenario":"%s","first":"%s"}',
                    payload.scenario_id, errs[0])
        result = await loop.run_in_executor(
            None, optimizer.safe_plan, hours, battery, directives)
        tier = result.pop("tier", "idle_fallback")
        errs = replay.validate(hours, battery, directives, result)
        if errs:
            # The directives themselves are unsatisfiable: keep the plan valid
            # under the base GridWise rules rather than returning a broken one.
            log.warning('{"event":"safe_plan_rejected","scenario":"%s"}',
                        payload.scenario_id)
            result = await loop.run_in_executor(
                None, optimizer.safe_plan, hours, battery, [])
            result.pop("tier", None)
            tier = "idle_fallback"

    if tier != "optimal":
        log.info('{"event":"fallback","scenario":"%s","tier":"%s"}',
                 payload.scenario_id, tier)

    body = {
        "scenario_id": payload.scenario_id,
        "directive_interpretation": directives,
        "hourly_plan": result["hourly_plan"],
        "total_grid_kwh": result["total_grid_kwh"],
        "total_cost_bdt": result["total_cost_bdt"],
        "peak_grid_kwh": result["peak_grid_kwh"],
        "plan_summary": _summarize(directives, result, tier),
    }
    _cache_put(key, body)
    return JSONResponse(status_code=200, content=body)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
