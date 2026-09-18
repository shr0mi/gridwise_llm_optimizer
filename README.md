# GridWise LLM — Smart Campus Energy Optimization

BUP CSE Fest 2026 Hackathon · Online Preliminary · LLM-Assisted Operator Directive Interpretation

An HTTP service that reads 1–3 natural-language campus operator notes, uses a language
model to convert them into machine-checkable structured directives, validates that output
deterministically, and returns a cost-optimal, fully valid 24-hour energy schedule.

**Base URL:** `<fill in the deployed URL>`

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Readiness probe → `{"status":"ok"}` |
| `POST` | `/optimize-energy` | Note interpretation + 24-hour optimized schedule |

Status codes: `200` success · `400` malformed JSON · `422` well-formed but semantically
invalid · `500` controlled internal error (no stack traces, no secrets).

---

## Quickstart (clean machine)

```bash
git clone <repo-url> && cd gridwise-llm
python -m venv .venv && . .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env          # then put your key in GEMINI_API_KEY
export GEMINI_API_KEY=...     # Windows PowerShell: $env:GEMINI_API_KEY="..."

uvicorn main:app --host 0.0.0.0 --port 8000
```

Verify:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

Run all 10 public sample cases against the running service:

```bash
python test_local.py --base-url http://localhost:8000
```

Expected: `interpretation 10/10   valid 10/10   avg cost ratio 1.0000`.

Solver-only check (no API key needed — replays the optimizer against the organizer's
ground-truth directives):

```bash
python test_local.py --offline
```

---

## Docker

```bash
docker build -t <registry>/gridwise-llm:1.0.0 .
docker run --rm -p 8000:8000 -e GEMINI_API_KEY=<your-key> <registry>/gridwise-llm:1.0.0
curl http://localhost:8000/health
```

The image binds `0.0.0.0`, reads `PORT` from the environment (default `8000`), runs as a
non-root user, and contains **no baked-in credentials** — the key is supplied at runtime.

Pullable fallback image: `<registry>/gridwise-llm:1.0.0`

---

## Environment variables

| Name | Required | Default | Meaning |
|---|---|---|---|
| `GEMINI_API_KEY` | yes | — | Google Gemini API key (`GOOGLE_API_KEY` also accepted) |
| `GEMINI_MODEL` | no | `gemini-2.5-flash` | Model used for note interpretation |
| `LLM_TIMEOUT_S` | no | `12` | Per-call model timeout before safe fallback |
| `REQUEST_BUDGET_S` | no | `25` | Whole-request budget, kept under the 30 s judge limit |
| `LOG_LEVEL` | no | `INFO` | Log verbosity |
| `PORT` | no | `8000` | Listen port (injected by Render/Railway) |
| `WEB_CONCURRENCY` | no | `2` | uvicorn worker count in the container |

No secret values appear in this repository, in the image, in logs, or in API responses.

---

## Architecture

```
request ──► Pydantic schema validation ──► 400 / 422 on bad input
                    │
                    ▼
        Gemini (gemini-2.5-flash, temperature 0, JSON-schema response)
        one call carrying all 1–3 notes + the battery + hourly context
                    │  untrusted structured output
                    ▼
        Deterministic guardrails  (llm.sanitize)
        type allowlist · one entry per note in index order · hours unique/
        ascending/0–23 · factor clamped to [0,1] · reserve clamped to capacity ·
        applies semantics forced · anything unusable demoted to no_op
                    │  validated directives
                    ▼
        LP optimizer (scipy HiGHS, 96 variables)  ──► globally optimal schedule
                    │
                    ▼
        Round-and-re-derive  ──► exact energy balance, exact end-of-day neutrality,
                                 totals recomputed from hourly_plan
                    │
                    ▼
                 200 JSON
```

**The language model is the interpreter.** It produces the `directive_interpretation`
that the optimizer consumes as hard constraints — it is not used for cosmetic text. The
`plan_summary` is generated deterministically from the solved plan.

### Directive types

| type | `structured_adjustment` | effect on the optimization |
|---|---|---|
| `solar_reduction` | `{hours, factor}` | `effective_solar[h] = solar[h] × factor` |
| `minimum_battery_reserve` | `{hours, minimum_energy_kwh}` | `E[h] ≥ max(base minimum, value)` |
| `no_charge_window` | `{hours}` | charge forced to 0 |
| `no_discharge_window` | `{hours}` | discharge forced to 0 |
| `max_grid_window` | `{hours, max_grid_kwh}` | `grid[h] ≤ value` |
| `no_op` | `null` | none (`applies = false`) |

Time windows are **start-inclusive, end-exclusive** — "1 PM to 3 PM" → `[13, 14]`.
`factor` is the **fraction remaining** — an 80% reduction → `0.2`.

### Optimizer

Minimize `Σ tariff[h] · grid[h]` over 96 continuous variables (grid, solar used, charge,
discharge per hour), subject to hourly energy balance, solar availability after any
`solar_reduction`, battery capacity and active minimum, hourly charge/discharge rate
limits, end-of-day battery neutrality, and any directive-imposed grid caps or
charge/discharge lockouts. Solved with HiGHS via `scipy.optimize.linprog`, so the result
is a **global optimum**, not a heuristic. Typical solve time: ~3 ms.

After solving, simultaneous charge/discharge is netted out, values are rounded to 6
decimals, `battery_energy_after_kwh` is accumulated from the rounded battery figures, and
`grid_kwh` is re-derived as `demand + charge − solar_used − discharge`. This makes the
energy-balance equation hold **exactly** rather than merely within tolerance when the
judge replays the plan.

### Safe failure

If the model times out, errors, is rate-limited, returns blocked or unparseable content,
or emits an unsupported directive type, the guardrails demote the affected note(s) to
`no_op` and the service still returns a valid 200 with a complete, rule-compliant
schedule. The service never invents a directive type and never crashes on model failure.
If directives were ever mutually infeasible, the optimizer falls back first to base
GridWise rules and finally to an always-valid idle-battery schedule.

`/health` is a static response and touches neither the model nor the solver, so it stays
up during a provider outage.

---

## Example

```bash
curl -X POST http://localhost:8000/optimize-energy \
  -H 'Content-Type: application/json' \
  -d '{
    "scenario_id": "GRID-101",
    "operator_notes": [
      "Solar output will drop to about 20% from 1 PM to 3 PM.",
      "The cafeteria menu changes tomorrow."
    ],
    "hours": [ {"hour": 0, "demand_kwh": 180, "solar_kwh": 0, "tariff_bdt_per_kwh": 7} ],
    "battery": {
      "capacity_kwh": 500, "initial_energy_kwh": 200, "minimum_energy_kwh": 50,
      "max_charge_kwh_per_hour": 100, "max_discharge_kwh_per_hour": 100
    }
  }'
```

(`hours` must carry all 24 entries; abbreviated above. A complete ready-to-send request is
`cases[0].input` in `public_cases.json`.)

Response shape:

```json
{
  "scenario_id": "GRID-101",
  "directive_interpretation": [
    {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
     "explanation": "Usable solar is reduced to 20% during the stated window."},
    {"note_index": 1, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null,
     "explanation": "This note does not affect today's 24-hour energy schedule."}
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 180.0, "solar_used_kwh": 0.0,
     "battery_action": "idle", "battery_kwh": 0.0, "battery_energy_after_kwh": 200.0}
  ],
  "total_grid_kwh": 0.0,
  "total_cost_bdt": 0.0,
  "peak_grid_kwh": 0.0,
  "plan_summary": "…"
}
```

---

## Files

| File | Role |
|---|---|
| `main.py` | FastAPI app, both endpoints, error handlers, request budget |
| `schemas.py` | Pydantic models for the exact request/response contract |
| `llm.py` | Gemini prompt + call, and the deterministic guardrails |
| `optimizer.py` | LP formulation, solve, and plan construction |
| `test_local.py` | Judge-equivalent harness over the 10 public sample cases |
| `public_cases.json` | Organizer-supplied public sample pack |
| `Dockerfile` / `render.yaml` | Container and Render service definition |

## Dependencies

`fastapi`, `uvicorn[standard]`, `pydantic` (API and validation) · `numpy`, `scipy`
(HiGHS linear programming) · `google-genai` (Gemini SDK). Exact versions in
`requirements.txt`. All are standard open-source libraries; the service architecture,
prompt, guardrails and optimizer formulation are our own work.

## Known limitations

- Interpretation quality depends on Gemini availability; on a provider outage notes
  degrade to `no_op` and the schedule is still valid, but directive credit for those
  notes is lost.
- A note that packs two different directives is mapped to the single supported type that
  best matches, per the specification that each note maps to exactly one type.
- The battery is modelled as lossless (no round-trip efficiency term), matching the
  energy rules in the problem statement.
- No response caching is enabled, so repeated identical scenarios each incur a model call.
