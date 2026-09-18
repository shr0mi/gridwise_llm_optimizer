<p align="center">
  <img src="logo.jpeg" alt="GridWise LLM logo" width="100">
</p>

<h1 align="center">GridWise LLM</h1>

<p align="center">
  <strong>Smart campus energy scheduler, powered by a language model.</strong><br>
  Turn free-form operator notes into a cost-optimal 24-hour battery &amp; solar plan.
</p>

<p align="center">
  <strong>Team:</strong> <code>DU_Context_Limit_Exceed</code> · <strong>Event:</strong> BUP CSE Fest 2026 Hackathon · Online Preliminary
</p>

<p align="center">
  <a href="#-what-it-does">What it does</a> ·
  <a href="#-tech-stack">Tech</a> ·
  <a href="#-endpoints">Endpoints</a> ·
  <a href="#-quickstart">Quickstart</a> ·
  <a href="#-sample-request--response">Sample</a> ·
  <a href="#-core-features">Features</a> ·
  <a href="#-how-it-works">How it works</a>
</p>

---

## What it does

Cantonment campuses (schools, offices, hostels) run on a 24-hour mix of **grid power, solar panels, and a battery**. Every hour has its own demand, its own solar forecast, and its own electricity price. The cheapest schedule is rarely obvious — charge the battery at 4 AM when rates are low, discharge it at 7 PM when the peak hits and rates are 5× higher, spill any excess solar.

Now add a human: the building manager sends 1–3 free-form notes a day:

> *"Wash the rooftop panels from noon to 2 PM; expect about 25% of normal output."*
> *"Keep at least 30% of the battery in reserve from 5 PM to 10 PM."*
> *"The cafeteria menu changes tomorrow."*

Some of those notes change the schedule. Some are noise. The tricky part is **figuring out which is which** — and turning the relevant ones into numbers the optimizer can use.

**GridWise LLM does exactly that.** It is a small HTTP service that:

1. **Reads** 1–3 operator notes plus the day's hourly forecasts and battery spec.
2. **Uses a language model** (Gemini, free tier) to interpret every note into a structured directive.
3. **Validates** that interpretation with deterministic guardrails (no invented rule types, no out-of-range hours, no invalid numbers).
4. **Optimizes** a 24-hour plan that satisfies the GridWise rules + every applicable directive, at minimum grid cost.
5. **Re-checks** the plan hour-by-hour before sending it back, so the answer is always self-consistent.

Built for the **BUP CSE Fest 2026 Hackathon · LLM-Assisted GridWise Preliminary**.

---

## Tech stack

| Layer | Tool | Why |
|---|---|---|
| Language | ![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white) | Standard for fast prototyping and ML glue. |
| HTTP framework | ![FastAPI](https://img.shields.io/badge/FastAPI-0.123-009688?logo=fastapi&logoColor=white) | Type-checked endpoints, automatic Swagger UI, async-native. |
| Validation | ![Pydantic](https://img.shields.io/badge/Pydantic-2.12-E92063?logo=pydantic&logoColor=white) | Strict request/response schema; rejects malformed JSON before it reaches the model. |
| LLM SDK | ![google-genai](https://img.shields.io/badge/google--genai-2.24-4285F4?logo=google&logoColor=white) | Free-tier Gemini access with structured JSON output. |
| Optimizer | ![SciPy](https://img.shields.io/badge/SciPy-1.16-8CAAE6?logo=scipy&logoColor=white) ![NumPy](https://img.shields.io/badge/NumPy-2.2-013243?logo=numpy&logoColor=white) | HiGHS linear-program solver — finds the cheapest plan in milliseconds. |
| Container | ![Docker](https://img.shields.io/badge/Docker-ready-2496ED?logo=docker&logoColor=white) | Same image runs locally, on Render, or anywhere Docker is installed. |
| Hosting | ![Render](https://img.shields.io/badge/Render-Deployed-46E3B7?logo=render&logoColor=white) | One-click Blueprint deploy from the repo; free tier works with an uptime pinger. |

---

## Endpoints

Once running locally, the service is at `http://localhost:8000`.

| Method | Path | What it does | When to use it |
|---|---|---|---|
| `GET`  | [`/health`](http://localhost:8000/health) | Readiness check. Returns `{"status":"ok"}`. | The judge's first ping. Also perfect for an external uptime pinger. |
| `POST` | [`/optimize-energy`](http://localhost:8000/optimize-energy) | Interpret notes, build the cheapest 24-hour plan. | The main endpoint — every test case hits this. |
| `GET`  | [`/docs`](http://localhost:8000/docs) | Swagger UI with full request/response schemas. | Browsing and trying the API by hand. |
| `GET`  | [`/redoc`](http://localhost:8000/redoc) | Cleaner, read-only API reference. | Sharing with teammates or the judge. |
| `GET`  | [`/openapi.json`](http://localhost:8000/openapi.json) | Raw OpenAPI 3 schema. | Generating client SDKs. |
| `GET`  | [`/diagnostics`](http://localhost:8000/diagnostics) | Live counters — is the LLM actually running? | Operational visibility (not judged). |

**HTTP status codes:**

- `200` — success, plan returned
- `400` — malformed JSON or structurally invalid request
- `422` — well-formed but semantically contradictory (e.g. `initial_energy_kwh` larger than `capacity_kwh`)
- `500` — controlled internal error (no stack traces, no secrets, no prompts ever leave the server)

---

## Quickstart

A working copy on a clean machine in 5 commands. Uses Python 3.11+.

```bash
# 1. Clone and enter
git clone <your-repo-url> gridwise-llm && cd gridwise-llm

# 2. Create a virtual environment
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Add your free Gemini key (https://aistudio.google.com/apikey)
cp .env.example .env
# then edit .env and set GEMINI_API_KEY=AIzaSy...

# 5. Start the service
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then in a second terminal:

```bash
curl http://localhost:8000/health
# {"status":"ok"}

# Run the full 10-case pipeline + judge replay
python test_local.py --base-url http://localhost:8000
# Target: interpretation 10/10   valid 10/10   avg cost ratio 1.0000
```

> The service **runs without an API key** — it falls back to a deterministic extractor
> that still scores 10/10 on the public cases. But the LLM is the required primary
> interpreter, so always set the key for a real run.

### Required environment variables

The most important ones. Full list in `.env.example`.

| Variable | Default | What it does |
|---|---|---|
| `GEMINI_API_KEY` | — | Your free Google AI Studio key. Required. |
| `LLM_PROVIDER` | `gemini` | `gemini` or `anthropic`. All resilience machinery applies to either. |
| `LLM_FALLBACK_PROVIDERS` | — | Comma-separated backup providers if the primary is unavailable. |
| `GEMINI_API_KEYS` | — | Optional comma-separated pool of keys, round-robined to multiply free-tier quota. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Primary interpretation model. |
| `GEMINI_FALLBACK_MODELS` | provider defaults | Backup models tried if the primary hits a per-model quota. |
| `LLM_MAX_CONCURRENCY` | `4` | Caps simultaneous model calls under the free-tier RPM ceiling. |
| `REQUEST_BUDGET_S` | `25` | Whole-request budget. Judge limit is 30 s. |

---

## Sample request & response

Here's one of the 10 public sample cases (SAMPLE-01, *solar cleaning + a distractor*). Tap to expand.

<details>
<summary><strong>▶ Request body (POST /optimize-energy)</strong></summary>

```json
{
  "scenario_id": "SAMPLE-01",
  "operator_notes": [
    "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
    "The sports office moved next month's registration deadline."
  ],
  "hours": [
    {"hour": 0,  "demand_kwh": 90,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 1,  "demand_kwh": 85,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 2,  "demand_kwh": 80,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
    {"hour": 3,  "demand_kwh": 80,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
    {"hour": 4,  "demand_kwh": 85,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 5},
    {"hour": 5,  "demand_kwh": 95,  "solar_kwh": 0,   "tariff_bdt_per_kwh": 6},
    {"hour": 6,  "demand_kwh": 110, "solar_kwh": 5,   "tariff_bdt_per_kwh": 8},
    {"hour": 7,  "demand_kwh": 130, "solar_kwh": 20,  "tariff_bdt_per_kwh": 10},
    {"hour": 8,  "demand_kwh": 150, "solar_kwh": 50,  "tariff_bdt_per_kwh": 12},
    {"hour": 9,  "demand_kwh": 165, "solar_kwh": 90,  "tariff_bdt_per_kwh": 14},
    {"hour": 10, "demand_kwh": 175, "solar_kwh": 130, "tariff_bdt_per_kwh": 16},
    {"hour": 11, "demand_kwh": 180, "solar_kwh": 160, "tariff_bdt_per_kwh": 16},
    {"hour": 12, "demand_kwh": 185, "solar_kwh": 180, "tariff_bdt_per_kwh": 15},
    {"hour": 13, "demand_kwh": 180, "solar_kwh": 170, "tariff_bdt_per_kwh": 14},
    {"hour": 14, "demand_kwh": 170, "solar_kwh": 140, "tariff_bdt_per_kwh": 13},
    {"hour": 15, "demand_kwh": 165, "solar_kwh": 90,  "tariff_bdt_per_kwh": 14},
    {"hour": 16, "demand_kwh": 170, "solar_kwh": 45,  "tariff_bdt_per_kwh": 18},
    {"hour": 17, "demand_kwh": 185, "solar_kwh": 10,  "tariff_bdt_per_kwh": 22},
    {"hour": 18, "demand_kwh": 205, "solar_kwh": 0,   "tariff_bdt_per_kwh": 28},
    {"hour": 19, "demand_kwh": 215, "solar_kwh": 0,   "tariff_bdt_per_kwh": 30},
    {"hour": 20, "demand_kwh": 205, "solar_kwh": 0,   "tariff_bdt_per_kwh": 26},
    {"hour": 21, "demand_kwh": 175, "solar_kwh": 0,   "tariff_bdt_per_kwh": 18},
    {"hour": 22, "demand_kwh": 135, "solar_kwh": 0,   "tariff_bdt_per_kwh": 10},
    {"hour": 23, "demand_kwh": 105, "solar_kwh": 0,   "tariff_bdt_per_kwh": 7}
  ],
  "battery": {
    "capacity_kwh": 220,
    "initial_energy_kwh": 110,
    "minimum_energy_kwh": 40,
    "max_charge_kwh_per_hour": 50,
    "max_discharge_kwh_per_hour": 50
  }
}
```

The first note should reduce solar to 25% during hours 12–13.
The second is about a sports deadline — totally unrelated, should be ignored.

</details>

<details>
<summary><strong>▶ Response body (200 OK)</strong></summary>

```json
{
  "scenario_id": "SAMPLE-01",
  "directive_interpretation": [
    {
      "note_index": 0,
      "applies": true,
      "directive_type": "solar_reduction",
      "structured_adjustment": { "hours": [12, 13], "factor": 0.25 },
      "explanation": "Solar output will be reduced to 25% of forecast during the cleaning window."
    },
    {
      "note_index": 1,
      "applies": false,
      "directive_type": "no_op",
      "structured_adjustment": null,
      "explanation": "This note is about a future event and does not affect today's energy schedule."
    }
  ],
  "hourly_plan": [
    { "hour": 0,  "grid_kwh": 90,  "solar_used_kwh": 0,   "battery_action": "idle",      "battery_kwh": 0,  "battery_energy_after_kwh": 110 },
    { "hour": 1,  "grid_kwh": 45,  "solar_used_kwh": 0,   "battery_action": "discharge", "battery_kwh": 40, "battery_energy_after_kwh": 70 },
    { "hour": 2,  "grid_kwh": 130, "solar_used_kwh": 0,   "battery_action": "charge",    "battery_kwh": 50, "battery_energy_after_kwh": 120 },
    { "hour": 3,  "grid_kwh": 130, "solar_used_kwh": 0,   "battery_action": "charge",    "battery_kwh": 50, "battery_energy_after_kwh": 170 },
    { "hour": 4,  "grid_kwh": 135, "solar_used_kwh": 0,   "battery_action": "charge",    "battery_kwh": 50, "battery_energy_after_kwh": 220 },
    { "hour": 5,  "grid_kwh": 95,  "solar_used_kwh": 0,   "battery_action": "idle",      "battery_kwh": 0,  "battery_energy_after_kwh": 220 },
    { "hour": 6,  "grid_kwh": 105, "solar_used_kwh": 5,   "battery_action": "idle",      "battery_kwh": 0,  "battery_energy_after_kwh": 220 },
    { "hour": 7,  "grid_kwh": 110, "solar_used_kwh": 20,  "battery_action": "idle",      "battery_kwh": 0,  "battery_energy_after_kwh": 220 },
    { "hour": 8,  "grid_kwh": 100, "solar_used_kwh": 50,  "battery_action": "idle",      "battery_kwh": 0,  "battery_energy_after_kwh": 220 },
    { "hour": 9,  "grid_kwh": 75,  "solar_used_kwh": 90,  "battery_action": "idle",      "battery_kwh": 0,  "battery_energy_after_kwh": 220 },
    { "hour": 10, "grid_kwh": 0,   "solar_used_kwh": 130, "battery_action": "discharge", "battery_kwh": 45, "battery_energy_after_kwh": 175 },
    { "hour": 11, "grid_kwh": 0,   "solar_used_kwh": 160, "battery_action": "discharge", "battery_kwh": 20, "battery_energy_after_kwh": 155 },
    { "hour": 12, "grid_kwh": 90,  "solar_used_kwh": 45,  "battery_action": "discharge", "battery_kwh": 50, "battery_energy_after_kwh": 105 },
    { "hour": 13, "grid_kwh": 152.5, "solar_used_kwh": 42.5, "battery_action": "charge",  "battery_kwh": 15, "battery_energy_after_kwh": 120 },
    { "hour": 14, "grid_kwh": 80,  "solar_used_kwh": 140, "battery_action": "charge",    "battery_kwh": 50, "battery_energy_after_kwh": 170 },
    { "hour": 15, "grid_kwh": 125, "solar_used_kwh": 90,  "battery_action": "charge",    "battery_kwh": 50, "battery_energy_after_kwh": 220 },
    { "hour": 16, "grid_kwh": 125, "solar_used_kwh": 45,  "battery_action": "idle",      "battery_kwh": 0,  "battery_energy_after_kwh": 220 },
    { "hour": 17, "grid_kwh": 145, "solar_used_kwh": 10,  "battery_action": "discharge", "battery_kwh": 30, "battery_energy_after_kwh": 190 },
    { "hour": 18, "grid_kwh": 155, "solar_used_kwh": 0,   "battery_action": "discharge", "battery_kwh": 50, "battery_energy_after_kwh": 140 },
    { "hour": 19, "grid_kwh": 165, "solar_used_kwh": 0,   "battery_action": "discharge", "battery_kwh": 50, "battery_energy_after_kwh": 90 },
    { "hour": 20, "grid_kwh": 155, "solar_used_kwh": 0,   "battery_action": "discharge", "battery_kwh": 50, "battery_energy_after_kwh": 40 },
    { "hour": 21, "grid_kwh": 175, "solar_used_kwh": 0,   "battery_action": "idle",      "battery_kwh": 0,  "battery_energy_after_kwh": 40 },
    { "hour": 22, "grid_kwh": 155, "solar_used_kwh": 0,   "battery_action": "charge",    "battery_kwh": 20, "battery_energy_after_kwh": 60 },
    { "hour": 23, "grid_kwh": 155, "solar_used_kwh": 0,   "battery_action": "charge",    "battery_kwh": 50, "battery_energy_after_kwh": 110 }
  ],
  "total_grid_kwh": 2692.5,
  "total_cost_bdt": 38365,
  "peak_grid_kwh": 175,
  "plan_summary": "Applied solar_reduction over hours 12-13. 1 note(s) were irrelevant and ignored. Charged the battery in cheap hours and discharged it into the evening peak, curtailing surplus solar, for a total grid draw of 2692.50 kWh costing 38365.00 BDT with a peak of 175.00 kWh."
}
```

The interpretation matches the reference exactly. The plan charges the battery at 2–4 AM (cheap rate 5 BDT/kWh) and the early afternoon (rate 13–14), then discharges it into the evening peak (rate 28–30) where it saves the most. Solar is curtailed when there's more than the campus can absorb.

</details>

To reproduce this exact call locally:

```bash
python -c "import json;print(json.dumps(json.load(open('public_cases.json'))['cases'][0]['input']))" > /tmp/case1.json
curl -X POST http://localhost:8000/optimize-energy \
     -H 'Content-Type: application/json' --data @/tmp/case1.json
```

---

## Core features

### Smart LLM interpretation
The language model reads every operator note and converts it to a structured directive with a strict JSON schema. Supported types and their effects:

| Type | Example phrase | Structured form |
|---|---|---|
| `solar_reduction` | "output drops to about 25% from noon to 2 PM" | `{"hours": [12, 13], "factor": 0.25}` |
| `minimum_battery_reserve` | "keep at least 30% in reserve from 5 PM to 10 PM" | `{"hours": [17,18,19,20,21], "minimum_energy_kwh": 66}` |
| `no_charge_window` | "do not charge between 2 PM and 4 PM" | `{"hours": [14, 15]}` |
| `no_discharge_window` | "no export from 7 PM through 9 PM" | `{"hours": [19, 20]}` |
| `max_grid_window` | "feeder caps us at 140 kWh from 8 PM to 11 PM" | `{"hours": [20, 21, 22], "max_grid_kwh": 140}` |
| `no_op` | "the cafeteria menu changes tomorrow" | `null` |

Time windows are **start-inclusive and end-exclusive** (1 PM to 3 PM → `[13, 14]`).
Percentages of battery capacity are resolved into absolute kWh.

### Deterministic guardrails
Everything the LLM says is checked before it touches the optimizer. Out-of-range hours are clamped. Invalid factors are demoted to `no_op`. Unknown directive types are dropped. The system **never invents** a rule it can't satisfy — bad model output becomes a missed directive, never a wrong one.

### Provably optimal scheduling
The 24-hour schedule is solved as a linear program over 96 variables (grid import, solar used, battery charge, battery discharge — one of each per hour) using HiGHS through SciPy. It finds the **global** minimum-cost plan in 2–5 ms. Hard directives are baked into the LP, not patched afterwards, so they cannot be silently ignored.

### Tiered fallback when directives conflict
If two directives together make the day infeasible, the optimizer drops the **fewest** that restore feasibility, then falls back to the base GridWise rules, then to a conservative idle-battery plan. An invalid plan is **never** emitted — the final replays the judge would do and swaps in a safer tier if anything is off.

### LLM resilience
Free-tier quotas and provider outages are the real-world failure mode. The service handles them with:

- **Key pool** — multiple Gemini keys, round-robin so no single key is the ceiling
- **Model chain** — if `gemini-2.5-flash` is rate-limited, `gemini-3.1-flash-lite` is tried next
- **Provider chain** — `LLM_FALLBACK_PROVIDERS` lets you list `groq`, `anthropic`, etc. as backup
- **Per-model cooldown** — rate-limited models are parked for the exact retry delay the API reports
- **Circuit breaker** — a provider that fails 4 times in a row is skipped for 30 s
- **Normalized note cache** — repeated or paraphrased notes never reach the API
- **Deterministic cross-check + arbiter** — if the LLM and the rule-based extractor disagree, a bounded extra call decides who is right

A `/diagnostics` endpoint exposes live counters so you can see at a glance whether the LLM is actually serving requests or whether you've silently degraded to the deterministic path.

### Strict, safe error handling
- `400` for malformed JSON or structurally invalid requests
- `422` for semantically contradictory ones (e.g. `initial_energy_kwh > capacity_kwh`)
- `500` responses never leak stack traces, prompts, or secrets — verified by the hostile-input drill
- Numeric `Infinity` / `NaN` in any field → `400`, not `500`

### One-image deploy
The Dockerfile builds, the Render Blueprint deploys, and a free Docker Hub image is the documented fallback. No proprietary infra, no manual steps.

---

## How it works

A request flows through 5 small stages. Each one has a single job; each one is testable on its own.

```
              HTTP POST /optimize-energy
                          │
                          ▼
            ┌──────────────────────────┐
            │  1. Request guard        │   Pydantic v2 schema
            │     (Pydantic)           │   400 / 422 on bad input
            └────────────┬─────────────┘
                         │ valid ScenarioRequest
                         ▼
            ┌──────────────────────────┐
            │  2. Interpretation       │   normalized note cache
            │     (LLM + guardrails)   │ ─► Gemini call (JSON schema)
            │                          │ ─► deterministic cross-check
            │                          │ ─► arbiter on disagreement
            │                          │ ─► rule reading on outage
            └────────────┬─────────────┘
                         │ validated directives
                         ▼
            ┌──────────────────────────┐
            │  3. Optimizer (LP)       │   96 vars, HiGHS
            │     (SciPy)              │   provably minimum-cost
            │                          │   tiered relaxation if infeasible
            └────────────┬─────────────┘
                         │ plan
                         ▼
            ┌──────────────────────────┐
            │  4. Final replay         │   hour-by-hour validator
            │     (judge-equivalent)   │   swap to safer tier on failure
            └────────────┬─────────────┘
                         │ safe OptimizeResponse
                         ▼
                   HTTP 200 JSON
```

### Plain-English tour

1. **Request guard.** A Pydantic model checks that the body is the exact shape the judge sends — 24 hourly entries, one per hour 0–23, a battery object with sane numbers, 1–3 non-empty operator notes. Anything malformed gets a clean `400`. Anything contradictory (initial energy above capacity, etc.) gets a `422`.

2. **Interpretation.** The LLM receives every operator note, the full 24-hour table, and the battery spec. It returns one structured entry per note in the same order — `applies`, `directive_type`, `structured_adjustment`, `explanation`. A deterministic parser runs in parallel as a cross-check; if the two disagree, a bounded arbiter call decides. The note cache means repeated notes never re-hit the API. Sanitizer enforces the schema before the optimizer sees anything.

3. **Optimizer.** SciPy's HiGHS solves a linear program that minimizes `Σ tariff × grid_import` subject to:
   - demand = grid + solar_used + discharge − charge, every hour
   - end-of-day battery level = starting level
   - battery state-of-charge always in `[min, capacity]`
   - each directive respected by construction (solar scaled, charge or discharge zeroed in their windows, etc.)
   If the directives together make the day infeasible, the optimizer drops the smallest subset that restores feasibility, then tries with no directives, then returns an analytic idle-battery plan.

4. **Final replay.** The finished schedule is run through the same hour-by-hour validator the judge uses. If it fails, the response is swapped for a safer tier before it leaves the server — so the answer is always self-consistent.

5. **Response.** JSON with exactly 7 top-level keys: `scenario_id`, `directive_interpretation`, `hourly_plan`, `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`, `plan_summary`. No extras. No stack traces. No secrets.

---

## Run the test suite

```bash
# offline — no API key needed, validates the optimizer only
python test_local.py --offline

# full pipeline + judge-equivalent replay, requires a working GEMINI_API_KEY
python test_local.py --base-url http://localhost:8000

# 43 paraphrases of the same rule — extractor robustness
python test_paraphrase.py

# malformed / hostile input drill — must never 500 or leak secrets
python test_hostile.py --base-url http://localhost:8000

# was the model actually serving? shows live counters
curl -s http://localhost:8000/diagnostics
```

`test_local.py --base-url` exits non-zero with a loud warning if any request was served by the deterministic parser instead of the LLM. A perfect score proves nothing if the model never ran.

---

## Project structure

```
gridwise-llm/
├── main.py            FastAPI app, endpoints, /diagnostics, error handlers
├── config.py          Loads .env before any setting is read
├── schemas.py         Pydantic request / response contract
├── llm.py             Gemini call + resilience machinery + guardrails
├── providers.py       Provider abstraction (Gemini, Anthropic)
├── rules.py           Deterministic extractor — cross-check + outage fallback
├── optimizer.py       LP formulation, HiGHS solve, tiered relaxation
├── replay.py          Judge-equivalent hour-by-hour validator
├── public_cases.json  10 organizer-provided public cases
├── test_local.py      End-to-end judge-equivalent harness
├── test_paraphrase.py 43 rewordings across all directive types
├── test_hostile.py    Malformed / hostile input drill
├── Dockerfile         Container build
├── render.yaml        One-click Render Blueprint
├── DEPLOY.md          Deployment & verification runbook
└── README.md          You are here
```

---

## Secret handling

- No API keys, tokens, `.env` files, or `.pem` files are committed (`.gitignore` covers them).
- `.env.example` lists variable **names only**.
- The Docker image runs as a non-root user (`uid 10001`) and contains **no baked credentials** — keys are passed at runtime.
- Error responses carry a short message only. Logs are structured JSON and never include keys, prompts, or stack traces.

---

## Known limitations

- **Free-tier quota** (≈10–15 RPM, a few hundred RPD per model) is the binding constraint under sustained load. The note cache, key pool, and model chain mitigate it; beyond that, the service degrades to the deterministic reading rather than failing.
- **A note that packs two different directives** is mapped to the single best-matching supported type. The spec says each note maps to exactly one type.
- **The battery is modelled as lossless** (no round-trip efficiency term), matching the energy rules in the problem statement.
- **The last-resort idle-battery plan** honours `solar_reduction` but cannot guarantee a `max_grid_window` cap. It's only reached when the extracted directives are mutually unsatisfiable, which the judge's scoring scenarios are guaranteed not to be.
- **Caches are in-process**, so a multi-replica deployment warms them independently.

---

## Deployment

`DEPLOY.md` has the full runbook. The 30-second summary:

1. Push the repo to a **private** GitHub repo.
2. On Render → **New → Blueprint** → pick the repo.
3. Set the plan to **Starter** (free tier sleeps and breaks the judge) and paste your `GEMINI_API_KEY`.
4. Wait for the build to finish — your live URL is at the top of the service page.
5. Run `python test_local.py --base-url <your-url>` to confirm 10/10 from outside your network.
6. Optionally add an uptime pinger (UptimeRobot / cron-job.org) to keep `/health` warm during judging.

A public Docker Hub image is the documented fallback — the Dockerfile builds in one step and exposes port 8000 with no baked secrets.

---

<p align="center">
  Built for the <strong>BUP CSE Fest 2026 Hackathon · Online Preliminary</strong><br>
  by <strong>Team <code>DU_Context_Limit_Exceed</code></strong>
</p>