"""GridWise LLM service - BUP CSE Fest 2026 online preliminary.

GET  /health           readiness probe
POST /optimize-energy  LLM note interpretation + 24-hour cost-optimal schedule
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

import llm
import optimizer
from schemas import ScenarioRequest

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format='{"level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
)
log = logging.getLogger("gridwise")

REQUEST_BUDGET_S = float(os.getenv("REQUEST_BUDGET_S", "25"))  # judge limit is 30s

app = FastAPI(title="GridWise LLM", version="1.0.0")


# ------------------------------------------------------------------ error handling


@app.exception_handler(RequestValidationError)
async def _on_validation_error(request: Request, exc: RequestValidationError):
    """Malformed JSON body -> 400; well-formed but semantically invalid -> 422."""
    malformed = any(e.get("type") == "json_invalid" for e in exc.errors())
    return JSONResponse(
        status_code=400 if malformed else 422,
        content={"error": "malformed request" if malformed else "invalid request",
                 "detail": exc.errors()[:10]},
    )


@app.exception_handler(Exception)
async def _on_unhandled(request: Request, exc: Exception):
    log.exception("unhandled error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"error": "internal error"})


# ----------------------------------------------------------------------- endpoints


@app.get("/health")
async def health() -> Dict[str, str]:
    """Static readiness response - never touches the model or the solver."""
    return {"status": "ok"}


def _summarize(directives: List[Dict[str, Any]], result: Dict[str, Any]) -> str:
    applied = [d for d in directives if d["applies"]]
    if applied:
        parts = []
        for d in applied:
            adj = d["structured_adjustment"] or {}
            hrs = adj.get("hours", [])
            span = f"hours {hrs[0]}-{hrs[-1]}" if len(hrs) > 1 else f"hour {hrs[0]}"
            parts.append(f"{d['directive_type']} over {span}")
        head = "Applied " + "; ".join(parts) + ". "
    else:
        head = "No operator note changed today's schedule. "
    skipped = len(directives) - len(applied)
    tail = (f"{skipped} note(s) were irrelevant and ignored. " if skipped else "")
    return (head + tail +
            f"Charged the battery in cheap hours and discharged it into the "
            f"evening peak, curtailing surplus solar, for a total grid draw of "
            f"{result['total_grid_kwh']:.2f} kWh costing "
            f"{result['total_cost_bdt']:.2f} BDT with a peak of "
            f"{result['peak_grid_kwh']:.2f} kWh.")


@app.post("/optimize-energy")
async def optimize_energy(payload: ScenarioRequest) -> JSONResponse:
    hours = [h.model_dump() for h in payload.hours]
    battery = payload.battery.model_dump()
    notes = payload.operator_notes

    try:
        directives = await asyncio.wait_for(
            llm.interpret(notes, hours, battery), REQUEST_BUDGET_S * 0.6)
    except asyncio.TimeoutError:
        log.warning("interpretation exceeded the request budget; using no_op")
        directives = llm.sanitize(None, notes, battery)

    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, optimizer.optimize, hours, battery, directives)

    tier = result.pop("tier", "optimal")
    if tier != "optimal":
        log.warning("optimizer fell back to tier=%s for %s", tier, payload.scenario_id)

    body = {
        "scenario_id": payload.scenario_id,
        "directive_interpretation": directives,
        "hourly_plan": result["hourly_plan"],
        "total_grid_kwh": result["total_grid_kwh"],
        "total_cost_bdt": result["total_cost_bdt"],
        "peak_grid_kwh": result["peak_grid_kwh"],
        "plan_summary": _summarize(directives, result),
    }
    return JSONResponse(status_code=200, content=body)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
