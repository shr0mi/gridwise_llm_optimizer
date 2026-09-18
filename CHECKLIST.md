# GridWise — Rubric Compliance Checklist

Every line traced to the **Problem Statement** (PS §) or the **Participant Guide &
Evaluation Rubric** (PG §). Status is what I verified on this build, with the
command that proves it.

`[x]` verified here · `[ ]` needs a human action before submission · `[~]` done but
worth re-checking after deploy.

**Verification commands** (server on `http://localhost:8000`):

```bash
python test_local.py --offline                        # optimizer vs organizer optimal
python test_local.py --base-url http://localhost:8000 # full pipeline + LLM-path gate
python test_paraphrase.py                             # 43 rewordings
python test_hostile.py --base-url http://localhost:8000
GRIDWISE_BASE_URL=http://localhost:8000 pytest        # 189 tests
curl -s http://localhost:8000/diagnostics             # is the LLM actually running?
```

Last full run: **10/10 interpretation · 10/10 valid · cost ratio 1.0000 · 43/43
paraphrases · 36/36 hostile · 189 pytest · `interpreted_by_llm: 10,
interpreted_by_fallback: 0`.**

---

## 0. Mandatory / disqualifying (PG §09)

| # | Requirement | Source | Status |
|---|---|---|---|
| 0.1 | LLM is in the operator-note interpretation path, producing the structured directives the optimizer consumes | PS §02, PG §04 | `[x]` `llm.interpret` calls the model first on every uncached note; its guard-railed output is the default reading |
| 0.2 | LLM is **not** used only for `plan_summary` / docs | PG §04, §09 | `[x]` `plan_summary` is built by deterministic code in `main._summarize`; the model never writes it |
| 0.3 | Hard-coded phrase matching is **not** the sole interpreter | PG §04 | `[x]` `rules.py` is a cross-check and outage path only; the model runs first and wins by default |
| 0.4 | **Proof the model actually ran** | PG §09 | `[x]` `/diagnostics` reports `interpreted_by_llm` / `_by_fallback`; `test_local.py` prints a warning banner and **exits non-zero** on a degraded run |

> A perfect 60/60 on the public cases is achievable with the model completely
> dead — the deterministic parser serves every note and nothing looks wrong.
> Item 0.4 is the only thing standing between that and a disqualified submission.

---

## 1. LLM Directive Interpretation — 25 pts (PG §07)

| # | Sub-criterion | Pts | Status |
|---|---|---|---|
| 1.1 | Relevance / `no_op` detection | 5 | `[x]` 8 distractor cases incl. energy-vocabulary traps ("evaluating new solar panels next quarter") |
| 1.2 | Correct `directive_type` | 5 | `[x]` all six types covered; 92/92 on `test.json`, 18/18 public |
| 1.3 | Correct affected hours | 5 | `[x]` start-inclusive/end-exclusive (PS §5.1); 16 time-expression cases |
| 1.4 | Numeric values / `structured_adjustment` shape | 5 | `[x]` 12 numeric cases; shape enforced by `llm.sanitize` |
| 1.5 | Paraphrase robustness | 5 | `[x]` 43/43 + the 4 gaps found in code review |

Trap coverage (PS §5.1, §11.4, PG §08 "Time & factor normalization"):

- `[x]` `1 PM to 3 PM` → `[13,14]`, **not** `[13,14,15]`
- `[x]` `01:00 to 04:00` → `[1,2,3]` — a colon means 24-hour clock, not 1 PM
- `[x]` `10 PM until 2 AM` → `[0,1,22,23]` — wraps midnight, still ascending
- `[x]` `until midnight` → last hour 23
- `[x]` `throughout the day` → 0–23 · `during the 7 PM hour` → `[19]`
- `[x]` `factor` is the fraction **remaining**: 80 % reduction → `0.2`
- `[x]` `Half the array is offline` → `0.5`, **not** `0.0` — a fraction beats an outage keyword
- `[x]` `50% of battery capacity` → absolute kWh, either side of the capacity word
- `[x]` hours unique, ascending, 0–23 (PS §5.1)

---

## 2. Directive Application & Constraint Correctness — 25 pts (PG §07)

| # | Sub-criterion | Pts | Status |
|---|---|---|---|
| 2.1 | Ground-truth directive application | 10 | `[x]` directives enter the LP as hard constraints, then are replayed |
| 2.2 | Energy balance / effective solar | 5 | `[x]` `replay.validate` checks every hour |
| 2.3 | Battery transitions, bounds, rate limits | 5 | `[x]` same |
| 2.4 | Action consistency, neutrality, non-negative | 5 | `[x]` `battery_kwh = 0` iff idle; `E[23] == E0` exactly |

- `[x]` `effective_solar[h] = solar[h] × factor` (PS §5.3)
- `[x]` `E_after[h] ≥ max(base_min, directive_min)` (PS §5.3)
- `[x]` charge = 0 / discharge = 0 in banned windows; `grid[h] ≤ max_grid_kwh`
- `[x]` `grid + solar_used + discharge == demand + charge` every hour (PS §9.5)
- `[x]` `0 ≤ solar_used ≤ effective_solar`, surplus curtailed, no export (PS §9.4)
- `[x]` final `battery_energy_after_kwh == initial_energy_kwh` (PS §9.6)
- `[x]` **Final replay** guardrail (PS §08) — `replay.py` runs on every plan before
  it is returned; a failing plan is swapped for a safer tier, never emitted

---

## 3. Optimization Quality — 10 pts (PG §07)

- `[x]` Exact LP (HiGHS) over 96 variables — global optimum, not a heuristic
- `[x]` **10/10 public cases reproduce the organizer optimal, delta 0.00**
- `[x]` Every `test.json` scored case double-solved: dual simplex **and** an
  independently written interior-point LP agree to < 0.02 BDT
- `[x]` Totals derived from the final rounded `hourly_plan` (PS §11.3)
- `[x]` Round-then-rederive so the judge's replay holds exactly, not within tolerance
- `[~]` **Known edge:** `optimizer.CAP_MARGIN` (1e-5) makes an *exactly* binding
  `max_grid_window` infeasible, dropping that case to a relaxed tier. Needs a
  cap of `demand − max_discharge` exactly to trigger. Not fixed — see §8.

---

## 4. API Contract & Schema — 10 pts (PG §07)

| # | Sub-criterion | Pts | Status |
|---|---|---|---|
| 4.1 | Both endpoints / status behaviour | 2 | `[x]` `GET /health` → `{"status":"ok"}`; `POST /optimize-energy` |
| 4.2 | Request validation | 2 | `[x]` 400 structural, 422 semantic (PS §6.1); 36/36 hostile |
| 4.3 | `directive_interpretation` schema / order / types | 3 | `[x]` exactly one entry per note, `note_index` 0..N−1, no gaps or dupes |
| 4.4 | `hourly_plan` + top-level response + `scenario_id` echo | 3 | `[x]` exactly 7 top-level keys, 24 hours, `scenario_id` echoed |

- `[x]` `applies = false` only for `no_op`; `structured_adjustment = null` only for `no_op` (PS §5.1)
- `[x]` `battery_action ∈ {charge, discharge, idle}`; `battery_kwh = 0` when idle (PS §10.3)
- `[x]` Non-finite numbers rejected with **400** — `allow_inf_nan=False` on all 8
  numeric fields. Previously `Infinity` demand/tariff/capacity returned **500**,
  and `Infinity` solar returned **200 with an invalid, cheaper-than-optimal plan**
- `[x]` Swagger UI `/docs`, ReDoc `/redoc`, `/openapi.json` — all 8 schemas,
  200/400/422/500 documented, request example present

---

## 5. Performance & Reliability — 10 pts (PG §07, §08)

| # | Sub-criterion | Pts | Status |
|---|---|---|---|
| 5.1 | Health readiness within 60 s | 2 | `[x]` `/health` is a static literal — no model, no solver |
| 5.2 | p95 latency ≤ 5 s | 3 | `[~]` 6.8 s p95 measured on a rate-limited free key; ~2 s when quota is fresh. **Add more keys** — see §8 |
| 5.3 | Stability / failure rate | 3 | `[x]` 0 failures across 36 hostile + 189 pytest + repeated-request checks |
| 5.4 | Malformed & provider-failure handling, secret safety | 2 | `[x]` verified with no key, a bogus key, and a live 429 storm |

- `[x]` Per-request timeout well under the judge's 30 s (`REQUEST_BUDGET_S=25`)
- `[x]` Never 5xx on an LLM problem — guardrails demote, fallbacks cover
- `[x]` **Per-model quota cooldown**: a 429 parks that model for the delay the API
  reports, instead of re-confirming the same limit on every request
- `[x]` Circuit breaker trips only on genuine provider failures, not on quota
- `[x]` No retry on 401/403/404 — unrecoverable, so fail straight to the fallback
- `[x]` Async clients cached per (key, event loop) — they bind to the creating loop
- `[x]` No key, prompt or stack trace in any response or log (scanned by the hostile suite)

---

## 6. Deployment & Docker Fallback — 10 pts (PG §07)

| # | Sub-criterion | Pts | Status |
|---|---|---|---|
| 6.1 | Endpoint reachability | 3 | `[ ]` **deploy, then verify from an outside network** |
| 6.2 | Pullable image reaching `/health` | 4 | `[ ]` **build, push, pull-and-run on a clean machine** |
| 6.3 | Clean startup / reproducibility | 2 | `[x]` `render.yaml` + `Dockerfile`; non-root, binds `0.0.0.0:$PORT` |
| 6.4 | No manual debugging needed | 1 | `[x]` `DEPLOY.md` is a copy-paste runbook |

- `[x]` Image contains no baked secrets (`docker run … env | grep -i gemini` → clean)
- `[ ]` **Render free tier sleeps** — a cold start can take 30–50 s and blow the
  30 s per-request limit. Put an uptime pinger on `/health` for the whole window
- `[ ]` Submit the **exact tag or digest**, and verify `docker pull` + `docker run`

---

## 7. Documentation & Local Reproducibility — 10 pts (PG §07)

| # | Sub-criterion | Pts | Status |
|---|---|---|---|
| 7.1 | Clean quickstart from a fresh environment | 3 | `[x]` `README.md` → Quickstart |
| 7.2 | Env var names + model/provider documented | 2 | `[x]` full table; `.env.example` has **names only** |
| 7.3 | Public-sample test procedure + expected result | 2 | `[x]` README Testing + `DEPLOY.md` §2 |
| 7.4 | LLM → guardrail → optimizer architecture explained | 1 | `[x]` README Architecture diagram |
| 7.5 | Docker pull/run fallback instructions | 1 | `[x]` README Docker + `DEPLOY.md` §6 |
| 7.6 | Dependencies, limitations, secret handling | 1 | `[x]` README Dependencies / Known limitations / Secret handling |

- `[ ]` **Clean-room check:** have a teammate follow the README verbatim on a
  machine that never built this. Any undocumented step costs points here.

---

## 8. Open items — human action required

1. `[ ]` **Add 2–3 more free Gemini keys from *different* Google Cloud projects**
   → `GEMINI_API_KEYS=k1,k2,k3`. Quota is 20 requests/minute **per model per
   project**. This is the single highest-value change left: it is what moves p95
   from ~7 s back under the 5 s full-marks threshold.
2. `[ ]` **Deploy + uptime pinger** (`DEPLOY.md` §4–5).
3. `[ ]` **Docker image**: build, push, verify pull-and-run from a clean machine.
4. `[ ]` **Repo public after the deadline**; confirm redistributing the organizer
   PDFs and `public_cases.json` in a public repo is acceptable, or gitignore them.
5. `[ ]` **3-minute video** — no base points, but it is the *first* tie-break (PG §10).
6. `[ ]` Secret scan before pushing:
   `git grep -nE "AIza[0-9A-Za-z_-]{20,}|AQ\.[A-Za-z0-9_-]{20,}|sk-ant-[A-Za-z0-9_-]{20,}"`
7. `[~]` **`CAP_MARGIN` edge (§3):** an exactly-binding `max_grid_window` is treated
   as infeasible. Low probability, but it silently costs a whole hidden case.
   Fix would be to solve at the exact cap and rely on the round-then-rederive
   step, or shrink the margin to 1e-9.
8. `[~]` **Anthropic provider is wired but never exercised against a live key.**
   `LLM_PROVIDER=anthropic` + `ANTHROPIC_API_KEY` switches providers; the schema
   translation and client construction are unit-tested, but no real call has been
   made. Test it before relying on it in the round.

---

## 9. Code-review items (HANDOFF_gridwise_review.md)

| Item | Verified? | Status |
|---|---|---|
| **P0-1** `Infinity` → 500 | Reproduced — and **worse than reported**: `Infinity` solar returned **200** with a plan costing 37 565 vs a true optimum of 38 365 | **Fixed** — all 8 numeric fields, 7 new hostile cases |
| **P0-2** Degraded run invisible | Reproduced | **Fixed** — `/diagnostics`, `STATS`, warning banner, non-zero exit |
| **P1-1** 4 paraphrase gaps | All 4 reproduced | **Fixed** — 4/4, no regression (43/43, 18/18, 92/92) |
| **P1-2** pytest collects 0 tests | Reproduced | **Fixed** — 189 tests |
| **P1-3** Gemini-only | Confirmed | **Fixed** — `providers.py`, `LLM_PROVIDER`; `effort` deliberately never sent (400 on Haiku 4.5 / Sonnet 4.5) |
| **P2** Arbiter outside the budget | Already fixed before the review landed | Arbiter shares the same absolute deadline and is skipped below 1.5 s remaining |
| **P2** Event-loop client caching | Was a real latent bug | **Fixed** — clients keyed by (key, loop) |
| **P2** Retrying unrecoverable statuses | Confirmed | **Fixed** — `_is_permanent_error` |
| **P2** `_scenario_key` includes `scenario_id` | Confirmed deliberate | No change; echoing the wrong id would fail the schema check |
| **§4** "Key is dead, 403 on all models" | **Not reproduced** | The key returns **429**, not 403. `gemini-3.5-flash` and `gemini-3.1-flash-lite` work. A full live run gives `interpreted_by_llm: 10, fallback: 0` |
| **§4** `gemini-2.5-flash` is 404 for new keys | **Not reproduced** — it works, but is rate-limited at 20 rpm | Kept as primary; chain reordered |

### Findings the review did not have

- **`Infinity` solar returned 200 with an invalid, cheaper-than-optimal plan.** A
  confidently wrong answer is worse than a 500 — a judge probing this would score
  a plan that draws 140 kWh of "solar" at midnight.
- **`gemini-flash-latest` shares `gemini-2.5-flash`'s quota bucket** — both return
  429 at the same instant. It was the *first* fallback, so the chain's first two
  entries died together. Removed.
- **Chain order was by model tier, not measured latency.** `gemini-3-flash-preview`
  takes 9.4 s against our prompt and timed out every call; `gemini-3.1-flash-lite`
  answers in 1.8 s. Reordered by measurement, slow model dropped.
- **Quota errors tripped the circuit breaker**, blacking out the LLM path for
  every request for 30 s. Quota is per model; only real provider failures trip it now.
- **The review's own secret-scan regex only matched `AIza…` keys** and would have
  missed the `AQ.…` key actually in use.
