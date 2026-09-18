# GridWise LLM — Smart Campus Energy Optimization

BUP CSE Fest 2026 Hackathon · Online Preliminary · LLM-Assisted Operator Directive Interpretation

An HTTP service that reads 1–3 natural-language campus operator notes, uses a language
model to convert them into machine-checkable structured directives, validates that output
deterministically, and returns a cost-optimal, fully valid 24-hour energy schedule.

**Base URL:** `<fill in the deployed URL>`
**Interactive API docs:** `<base-url>/docs` (Swagger UI) · `<base-url>/redoc`

---

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Readiness probe → `{"status":"ok"}` |
| `POST` | `/optimize-energy` | Note interpretation + 24-hour optimized schedule |
| `GET` | `/docs`, `/redoc`, `/openapi.json` | Swagger UI, ReDoc, raw OpenAPI schema |
| `GET` | `/diagnostics` | Operational counters — is the LLM actually running? Not part of the judged contract |

Status codes: `200` success · `400` malformed JSON **or structurally invalid request** ·
`422` well-formed but semantically contradictory (e.g. `initial_energy_kwh` above
`capacity_kwh`) · `500` controlled internal error — no stack traces, no secrets, no prompts.

---

## Quickstart (clean machine)

```bash
git clone <repo-url> && cd gridwise-llm
python -m venv .venv
. .venv/bin/activate                 # Windows PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Set one environment variable — a **free** Google AI Studio key from
<https://aistudio.google.com/apikey>:

```bash
export GEMINI_API_KEY="your-free-key"        # PowerShell: $env:GEMINI_API_KEY="..."
```

Start the service:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

Verify:

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}

python test_local.py --base-url http://localhost:8000
# expect: interpretation 10/10   valid 10/10   avg cost ratio 1.0000
```

One sample request end to end:

```bash
python -c "import json;print(json.dumps(json.load(open('public_cases.json'))['cases'][0]['input']))" > /tmp/case1.json
curl -s -X POST http://localhost:8000/optimize-energy \
     -H 'Content-Type: application/json' --data @/tmp/case1.json | head -c 600
```

> The service **runs without an API key**, falling back to its deterministic extractor.
> That path scores 10/10 on the public cases, but the LLM is the required primary
> interpreter — set the key for the real run.

---

## Environment variables

Names only; never commit values. Full list with defaults in `.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` or `anthropic`. All resilience machinery applies to either. |
| `GEMINI_API_KEY` | — | Free Google AI Studio key. Required for the LLM path. |
| `ANTHROPIC_API_KEY` | — | Used when `LLM_PROVIDER=anthropic`. |
| `LLM_MODEL` / `LLM_FALLBACK_MODELS` | provider defaults | Override the chain for either provider. |
| `LLM_MODEL_COOLDOWN_S` | `60` | How long a rate-limited model is parked before being retried. |
| `GEMINI_API_KEYS` | — | Optional comma-separated pool, round-robined to multiply free-tier RPM headroom. |
| `GEMINI_MODEL` | `gemini-2.5-flash` | Primary interpretation model. |
| `GEMINI_FALLBACK_MODELS` | `gemini-flash-latest,gemini-3.5-flash,gemini-3.1-flash-lite` | Tried in order when a per-model free-tier cap is hit. |
| `LLM_TIMEOUT_S` | `12` | Per model call. |
| `LLM_MAX_ATTEMPTS` | chain length | Retries across keys and models on 429 / 5xx; defaults to one attempt per model. |
| `LLM_MAX_CONCURRENCY` | `4` | Caps in-flight model calls under the free-tier RPM ceiling. |
| `LLM_ARBITER` | `1` | One extra call when the model and the rule reading disagree. |
| `LLM_CACHE_SIZE` | `20000` | Normalized operator-note cache entries. |
| `LLM_BREAKER_THRESHOLD` / `LLM_BREAKER_COOLDOWN_S` | `4` / `30` | Circuit breaker around the provider. |
| `REQUEST_BUDGET_S` | `25` | Whole-request budget; the judge limit is 30 s. |
| `SCENARIO_CACHE_SIZE` / `SCENARIO_CACHE_TTL_S` | `2000` / `3600` | Full-response cache for repeated scenarios. |
| `LOG_LEVEL`, `PORT`, `WEB_CONCURRENCY` | `INFO`, `8000`, `2` | Service settings. |

**Model / provider disclosure:** a **provider chain**, primary first:

1. **OpenAI** — `gpt-5.4-mini`, then `gpt-5.4-nano`. Chat completions with strict
   `json_schema` structured output, temperature default, called over `httpx`
   (the `openai` SDK 3.16 raises `RecursionError` inside
   `ssl.SSLContext.verify_mode` when `httpx` and `httpx2` are both installed).
2. **Google Gemini** — `gemini-2.5-flash`, then `gemini-3.1-flash-lite`,
   `gemini-3.5-flash`, `gemini-3.5-flash-lite`. **Free tier.**

Key pools, circuit breakers and rate-limit cooldowns are tracked **per provider**,
so an exhausted or dead primary degrades to the next provider rather than to no
LLM at all. Set `LLM_PROVIDER` / `LLM_FALLBACK_PROVIDERS` to reorder.

Legacy note: Google Gemini via the `google-genai` SDK, **free tier**.
Primary `gemini-2.5-flash`, temperature 0, JSON structured output with an enforced
response schema, thinking budget 0. Arbiter calls use the same chain. The fallback
models were probed against a live free-tier key; `gemini-2.5-flash-lite`,
`gemini-2.0-flash` and `gemini-2.5-pro` return 404 for newly created keys and are
deliberately absent from the chain.

---

## Architecture

```
POST /optimize-energy
        │
   ┌────▼─────────┐   400 structural / 422 semantic
   │ Request guard│   (Pydantic v2, exact contract)
   └────┬─────────┘
        │ notes[]                      hours + battery (never touched by the LLM)
   ┌────▼──────────────────────────┐            │
   │ INTERPRETATION                │            │
   │  1. normalized note cache     │            │
   │  2. Gemini call (JSON schema) │ ← MANDATORY│
   │  3. deterministic cross-check │            │
   │  4. arbiter on disagreement   │            │
   │  5. rule reading on outage    │            │
   └────┬──────────────────────────┘            │
        │ candidate directives                  │
   ┌────▼─────────┐  type allowlist · one entry per note · hours unique/ascending
   │  GUARDRAILS  │  0–23 · factor ∈ [0,1] · reserve ≤ capacity · caps finite
   │(deterministic)│  applies semantics forced · demote, never invent
   └────┬─────────┘
        │ validated directives ─────────────┐   │
   ┌────▼───────────────────────────────────▼───▼──┐
   │ OPTIMIZER — LP over 96 vars, HiGHS             │
   │ provably optimal · ~2–5 ms · tiered relaxation │
   └────┬───────────────────────────────────────────┘
        │ plan
   ┌────▼─────────┐  judge-equivalent hour-by-hour replay
   │ FINAL REPLAY │  fail ⇒ safer tier; an invalid plan is never emitted
   └────┬─────────┘
        │
   200 JSON (exactly 7 top-level keys)
```

### Directive types

| Type | `structured_adjustment` | Effect on the model |
|---|---|---|
| `solar_reduction` | `{"hours":[...], "factor":0..1}` | `effective_solar[h] = solar[h] × factor` |
| `minimum_battery_reserve` | `{"hours":[...], "minimum_energy_kwh":n}` | `E_after[h] ≥ max(base_min, n)` |
| `no_charge_window` | `{"hours":[...]}` | `charge[h] = 0` |
| `no_discharge_window` | `{"hours":[...]}` | `discharge[h] = 0` |
| `max_grid_window` | `{"hours":[...], "max_grid_kwh":n}` | `grid[h] ≤ n` |
| `no_op` | `null` | none; `applies = false` |

Time windows are **start-inclusive, end-exclusive** — "1 PM to 3 PM" → `[13, 14]`.
For `solar_reduction`, `factor` is the fraction that **remains usable** — an 80 %
reduction is `0.2`. Percentages of battery capacity are resolved into absolute kWh, which
is why the battery object is included in the prompt.

### LLM role (mandatory requirement)

The language model performs the interpretation itself: it receives the notes, the battery
object and the hourly table, and returns the structured directives that the optimizer
consumes. It is not used for `plan_summary` or documentation. `rules.py` is a
deterministic **cross-check and outage fallback**, never the sole interpreter — the model
runs first on every uncached note and its guard-railed output is the default reading.

### Optimizer

Linear program over 96 variables (grid, solar used, charge, discharge per hour), solved
with HiGHS through `scipy.optimize.linprog`:

```
minimize   Σ tariff[h] · grid[h]
s.t.       grid[h] + solar[h] + discharge[h] − charge[h] = demand[h]      (24 equalities)
           Σ (charge[h] − discharge[h]) = 0                              (end-of-day neutrality)
           active_min[h] ≤ E0 + Σ_{k≤h}(charge[k] − discharge[k]) ≤ capacity
bounds     0 ≤ grid[h] ≤ grid_cap[h] · 0 ≤ solar[h] ≤ eff_solar[h]
           0 ≤ charge[h] ≤ charge_cap[h] · 0 ≤ discharge[h] ≤ discharge_cap[h]
```

LP gives the **global** optimum, and the hard directives are satisfied by construction
rather than patched afterwards. After solving, simultaneous charge+discharge is netted
out, values are rounded to 6 dp, and the state-of-charge trajectory and grid draw are
**re-derived from the rounded numbers** — so the judge's replay holds exactly, not merely
within tolerance. Totals are computed from the final `hourly_plan`, which is the single
source of truth.

Verified: reproduces the organizer optimal cost on **10/10 public cases, delta 0.00**.

### Safe failure

Never a 500 on an interpretation problem, never an invented directive type, never an
invalid plan:

| Failure | Behaviour |
|---|---|
| Malformed / unparseable model output | Guardrails demote the affected note to `no_op` |
| Timeout, 429, quota exhausted, provider 5xx | Retry across keys and models, then the deterministic reading |
| Repeated provider failure | Circuit breaker opens; rule path until a probe succeeds |
| Extracted directives mutually infeasible | Relax the fewest directives that restore feasibility, then base rules, then an analytic idle-battery plan |
| Optimized plan fails the final replay | Swapped for a safer tier before the response is built |
| Malformed request | Controlled `400`/`422` with the raw input stripped from the detail |

---

## Testing

```bash
pytest                                                # 177 offline tests
GRIDWISE_BASE_URL=http://localhost:8000 pytest        # 189, incl. live contract
python test_local.py --offline                        # optimizer vs organizer optimal
python test_paraphrase.py                             # 43 rewordings, extractor only
python test_hostile.py --base-url http://localhost:8000   # malformed input drill
python test_local.py --base-url http://localhost:8000     # full pipeline + LLM-path gate
curl -s http://localhost:8000/diagnostics             # did the model actually run?
```

`test_local.py --base-url` **exits non-zero and prints a warning banner** if the
deterministic parser served any request. A perfect score proves nothing if the
model never ran, and the rubric disqualifies a submission whose LLM is not in the
interpretation path.

Measured on this build:

| Check | Result |
|---|---|
| Public cases, offline optimizer | 10/10 valid, cost ratio 1.0000, p95 4 ms |
| Public cases, full HTTP pipeline (live OpenAI) | 10/10 interpretation, 10/10 valid, ratio 1.0000, **p95 2.99 s** |
| Paraphrase suite (deterministic) | 43/43 |
| Paraphrase suite (live LLM path) | 43/43 |
| 24 concurrent requests | 24/24 correct, 0 errors, p95 126 ms |
| Cross-provider failover (OpenAI key killed) | Gemini took over, 10/10 |
| Hostile input suite | 36/36, zero 5xx, no leaked values |
| Pytest gate | 189 passed |
| Local test pack (`test.json`) | 92/92 interpretation notes |
| Provider-failure drill (no key / bad key) | 10/10 valid, 43/43 paraphrases |
| Free-tier rate-limit drill (quota exhausted mid-run) | 10/10 valid — deterministic reading took over |

`DEPLOY.md` has the full deployment and verification runbook.

---

## Docker

```bash
docker build -t gridwise-llm:latest .
docker run --rm -p 8000:8000 -e GEMINI_API_KEY="your-free-key" gridwise-llm:latest
curl -s http://localhost:8000/health
```

The image binds `0.0.0.0`, honours `$PORT`, runs as a non-root user (`uid 10001`) and
contains **no baked secrets** — the key is supplied at runtime only.

---

## Files

| File | Role |
|---|---|
| `main.py` | FastAPI app, both endpoints, `/diagnostics`, Swagger metadata, error handlers, scenario cache |
| `config.py` | Loads `.env` into the environment before any setting is read |
| `providers.py` | Gemini and Anthropic behind one interface, selected by `LLM_PROVIDER` |
| `schemas.py` | Pydantic models for the exact request/response contract |
| `llm.py` | Gemini prompt and call (free-tier key pool, retries, cache, arbiter) + guardrails |
| `rules.py` | Deterministic extractor: cross-check and provider-outage fallback |
| `optimizer.py` | LP formulation, solve, plan construction, tiered relaxation |
| `replay.py` | Judge-equivalent validator run on every plan before it is returned |
| `test_local.py` | Judge-equivalent harness over the 10 public sample cases |
| `test_paraphrase.py` | 43 rewordings across all six directive types |
| `test_hostile.py` | Malformed and hostile request drill |
| `tests/test_gridwise.py` | Pytest regression gate (189 tests) |
| `test.json` | 105-case local test pack with expected outputs |
| `CHECKLIST.md` | Rubric compliance checklist traced to both organizer PDFs |
| `public_cases.json` | Organizer-supplied public sample pack |
| `Dockerfile` / `render.yaml` | Container and Render service definition |
| `DEPLOY.md` | Deployment and verification runbook |

## Dependencies

`fastapi`, `uvicorn[standard]`, `pydantic` (API and validation) · `numpy`, `scipy`
(HiGHS linear programming) · `google-genai` (Gemini SDK). Exact pinned versions in
`requirements.txt`. All are standard open-source libraries; the service architecture,
prompt, guardrails, extractor and optimizer formulation are our own work.

## Secret handling

- No key, token or `.env` file is committed; `.gitignore` covers `.env`, `*.key`, `*.pem`.
- `.env.example` lists variable **names only**.
- Secrets are injected at runtime from the hosting dashboard; the Docker image has none.
- Error responses carry a short message only. Logs are structured JSON and never include
  keys, prompts or stack traces — verified by the hostile-input drill.

## Known limitations

- Free-tier quota (roughly 10–15 requests/minute, a few hundred per day per model) is the
  binding constraint under sustained load. The note cache, key pool and model chain
  mitigate it; beyond that the service degrades to the deterministic reading rather than
  failing.
- A note that packs two different directives is mapped to the single supported type that
  best matches, per the specification that each note maps to exactly one type.
- The battery is modelled as lossless (no round-trip efficiency term), matching the energy
  rules in the problem statement.
- The last-resort idle-battery plan honours `solar_reduction` but cannot guarantee a
  `max_grid_window` cap; it is only reached when the extracted directives are mutually
  unsatisfiable, which organizer scoring scenarios are guaranteed not to be.
- Caches are in-process, so a multi-replica deployment warms them independently.
