# GridWise — Code Review & Fix List

**To:** @shr0mi
**Re:** `gridwise_llm_optimizer` @ `28ba5ac`
**Decision: your implementation is our submission base.** Below is everything I'd fix before we submit, in priority order.

---

## 1. Verdict

I built a parallel implementation and scored both with an independent, judge-style harness (replays the returned `hourly_plan` under the **organizer's** ground-truth directives, not the team's own interpretation). Both hit **60/60** on the core automated subtotal with a **0.00 cost gap** on all 10 public cases.

Since the public cases don't separate them, I scored everything else. **Yours wins.**

| Dimension | Mine | Yours | Winner |
|---|---|---|---|
| Public cases (independent harness) | 60/60 | 60/60 | tie |
| LP optimality (all 10 cases) | exact | exact | tie |
| Infeasibility relaxation | fixed priority order | **minimal-subset search** | **yours** |
| Your hostile suite (29 checks) | 29/29 | 29/29 | tie |
| My hostile probes (10) | 10/10 | 9/10 — `Infinity` → 500 | mine |
| Paraphrase, deterministic path (shared 17-case set) | 15/17 | 13/17 | mine |
| LLM resilience | 2 retries, 1 provider | **key pool + breaker + model chain + note cache + arbiter** | **yours, decisively** |
| Concurrency, 24 parallel | 0 fail, 1.96 s | **0 fail, 0.04 s** | **yours** |
| Deployment readiness | Dockerfile only | **Dockerfile + `render.yaml` + 373-line runbook** | **yours** |
| Automated test suite | **61 pytest tests** | standalone scripts | mine |
| Provider portability | **Gemini + Anthropic, swappable** | Gemini only | mine |

### Why yours won

- **You solved the problem we actually have.** Our Gemini key is dead (details in §4). Your rotating key pool, circuit breaker, model fallback chain, free-tier-aware concurrency limit, and normalized note cache are built precisely for that failure mode. Mine wasn't.
- **Deployment is 10 rubric points and yours is ready.** `render.yaml` plus a runbook that already anticipates Render's free-tier cold start blowing the judge's 30 s limit. Mine still has `<REGISTRY>` placeholders.
- **The arbiter is the best idea in either codebase.** Running the LLM *and* the deterministic reading, then spending one bounded extra call when they disagree, catches the case where the model is *up but wrong* — which is exactly where interpretation marks are lost. Mine only falls back when the LLM is unreachable.
- **The caching and `run_in_executor` split is a genuine latency win** — 0.04 s vs my 1.96 s on 24 concurrent requests.

Nice work. The fixes below are polish on a strong base, not a rewrite.

---

## 2. Fix list

### P0 — blocks qualification

#### P0-1. `Infinity` in a numeric field returns HTTP 500

**Severity:** highest. `Performance & Reliability` explicitly scores "valid requests should not return 5xx" and "malformed input: return a controlled error or safe failure; do not crash."

**Reproduce:**
```bash
python - <<'PY'
import json, httpx
case = json.load(open("public_cases.json"))["cases"][0]["input"]
body = json.dumps(case).replace('"demand_kwh": 90', '"demand_kwh": Infinity', 1)
r = httpx.post("http://127.0.0.1:8000/optimize-energy", content=body,
               headers={"Content-Type": "application/json"}, timeout=20)
print(r.status_code, r.text[:80])
PY
```
Actual: `500 {"error":"internal error"}`. Expected: `400`.

**Root cause:** `schemas.py` lines 40–43 and 52–56. Pydantic v2 floats accept `inf`/`nan` unless told otherwise, and `+Infinity >= 0` passes `Field(ge=0)`. (`NaN` happens to be caught because `NaN >= 0` is `False` — that's luck, not validation.) The value flows into `optimizer._solve_lp` and SciPy raises:

```
ValueError: Invalid input for linprog: b_eq must not contain values inf, nan, or None
```

**Fix** — add `allow_inf_nan=False` to every numeric `Field` in `HourEntry` and `Battery`:

```python
# schemas.py — HourEntry
demand_kwh: float = Field(ge=0, allow_inf_nan=False, description="Campus demand for this hour.")
solar_kwh: float = Field(ge=0, allow_inf_nan=False, ...)
tariff_bdt_per_kwh: float = Field(allow_inf_nan=False, ...)

# schemas.py — Battery
capacity_kwh: float = Field(gt=0, allow_inf_nan=False, ...)
initial_energy_kwh: float = Field(ge=0, allow_inf_nan=False, ...)
minimum_energy_kwh: float = Field(ge=0, allow_inf_nan=False, ...)
max_charge_kwh_per_hour: float = Field(ge=0, allow_inf_nan=False, ...)
max_discharge_kwh_per_hour: float = Field(ge=0, allow_inf_nan=False, ...)
```

Then add to `test_hostile.py`: `+Infinity` demand, `-Infinity` solar, `Infinity` tariff, `Infinity` capacity — all expecting `400`.

---

#### P0-2. A dead API key still scores 60/60 — silently

**Severity:** highest. This one nearly cost me the round, so read it even though your code is well structured.

My first live run against Gemini printed a perfect **60/60** while the provider was returning **403 on every single call**. The deterministic path quietly served all 10 cases and the score looked flawless. I only caught it because I was reading the logs.

That matters because of the rubric's hardest line:

> Required LLM absent from operator-note interpretation path → **fails the mandatory challenge requirement; not eligible for the final preliminary shortlist.**

Your code has the same blind spot: `main.py` computes `tier` and logs `{"event":"fallback"}`, but nothing aggregates it, so a fully-degraded run is invisible unless someone greps the logs at the right moment.

**Fix** — two small pieces.

**(a) Counters + a diagnostics endpoint** in `main.py`:

```python
STATS = {"llm": 0, "fallback": 0, "last_error": None}

@app.get("/diagnostics", include_in_schema=False)
async def diagnostics() -> dict:
    """Operational visibility. Not part of the judged contract.
    Returns provider/model identifiers only — never a credential."""
    return {
        "model_chain": llm.MODEL_CHAIN,
        "keys_configured": len(llm._api_keys()),
        "llm_available": llm.llm_available(),
        "interpreted_by_llm": STATS["llm"],
        "interpreted_by_fallback": STATS["fallback"],
        "last_llm_error": STATS["last_error"],
        "cache": llm.cache_stats(),
    }
```

Increment `STATS["llm"]` when `llm.interpret` used a real model call, and `STATS["fallback"]` on the `asyncio.TimeoutError` branch and on any breaker-open / no-key path. `llm.interpret` already tracks `used_fallback` internally — surface it on the return value instead of discarding it.

**(b) Make the harness refuse to report a clean pass on a degraded run.** In `test_local.py`, after printing the rubric estimate, fetch `/diagnostics` and if `interpreted_by_fallback > 0`, print a loud banner **and exit non-zero**:

```
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!!  WARNING: the deterministic parser served some or all of these cases.
!!  Last LLM error: HTTP 403
!!  This score does NOT demonstrate a working LLM path, and the rubric
!!  requires the LLM to be in the interpretation path.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
```

Without this, we cannot tell a passing submission from a disqualifying one.

---

### P1 — costs points

#### P1-1. Four paraphrase gaps in `rules.py`

Tested on a shared 17-case set (your 43-case suite + mine + fresh hidden-style wordings). Yours scored 13/17. Exact failures:

| Note | Expected | Yours returned |
|---|---|---|
| "Battery must **hold 60% of capacity** from 7 PM to 10 PM." | `minimum_battery_reserve [19,20,21] 120` | `no_op` |
| "**Hold no fewer than** 95 kWh in the pack from 5 PM to 8 PM." | `minimum_battery_reserve [17,18,19] 95` | `no_op` |
| "Please **avoid drawing down the pack** from 16:00 to 18:00." | `no_discharge_window [16,17]` | `no_op` |
| "**Half** the array is offline for rewiring from 9 AM to 11 AM." | `solar_reduction [9,10] 0.5` | `solar_reduction [9,10] **0.0**` |

The first three are missing cue vocabulary — cheap to fix:

- **Reserve cues:** add `hold`, `maintain`, `retain`, `remain`, `stay`, `no fewer than`, `no lower than`, `not drop below`.
- **Discharge cues:** add `draw down`, `drawing down`, `drain`, `deplete`, and treat `pack` as a synonym for `battery`.
- **Percent-of-capacity reserve:** match the percentage on *either* side of the capacity word, then multiply by `capacity_kwh`. Mine uses:
  ```python
  r"(\d+(?:\.\d+)?)\s*(?:%|percent)[^.]{0,40}capacit|capacit\w*[^.]{0,40}?(\d+(?:\.\d+)?)\s*(?:%|percent)"
  ```

**The fourth is the dangerous one.** `factor: 0.0` is worse than `no_op`: it's a *wrong constraint that gets applied*, so we lose the interpretation mark **and** the downstream-application mark, and the plan is optimized against solar that doesn't exist. "offline"/"outage" is being read as a total blackout while the qualifier "Half" is ignored. Make a fraction word anywhere in the note take precedence over an outage keyword, and treat a bare outage word as a full reduction **only** when no fraction or percentage is present.

Mine handles all four except the "Half the array" one — that one beat both of us, so it's worth a test either way.

#### P1-2. No automated regression gate

`test_local.py`, `test_paraphrase.py`, and `test_hostile.py` are standalone scripts with `argparse` mains — `pytest` collects **zero** tests from them. They work, but only if someone remembers to run all three with the right flags.

Wrap the assertions as `pytest` functions (keep the `__main__` blocks so the CLI still works). Use `@pytest.mark.parametrize` over the 10 cases and the 43 paraphrases so a failure names the exact case. For reference my suite is 61 tests / 674 LOC and runs in ~2 s; that's what makes it safe to refactor under time pressure at 9 PM.

#### P1-3. Gemini is a single point of failure

`llm.py` is Gemini-only. Our key is dead, and if we end up buying Anthropic credits (§4) there is currently no path to use them.

Split `llm.py` into a thin facade plus two provider modules selected by `LLM_PROVIDER`. Keep all your resilience machinery — pool, breaker, chain, cache, arbiter — in the facade so it applies to whichever provider is active. I have this working; take `app/llm/client.py`, `app/llm/base.py`, `app/llm/gemini_provider.py`, `app/llm/anthropic_provider.py` from my tree and adapt.

**If you port the Anthropic provider, note this trap:** `output_config.effort` is **rejected with a 400 by Haiku 4.5 and Sonnet 4.5**. Sending it unconditionally turns every request into an error. Gate it:

```python
_NO_EFFORT_PREFIXES = ("claude-haiku", "claude-3", "claude-sonnet-4-5",
                       "claude-opus-4-1", "claude-opus-4-0")

def supports_effort(model: str) -> bool:
    return not model.startswith(_NO_EFFORT_PREFIXES)
```

---

### P2 — worth doing if time allows

- **Total-deadline safety.** Your `INTERPRET_BUDGET_S` (15 s) is enforced by `asyncio.wait_for`, which is the right shape. Double-check that `MAX_ATTEMPTS=3 × TIMEOUT_S=12` plus jittered backoff can't exceed it on the *arbiter* call path too — the arbiter is a second model call and I couldn't convince myself it's inside the same budget. Judge hard limit is 30 s; anything over is scored as a failure.
- **Client caching across event loops.** If you ever cache an async client at module level, key it by the running loop — an `AsyncClient`/`genai` client binds to the loop that created it and throws on reuse from another. Bit me in both my providers.
- **Don't retry unrecoverable statuses.** 401/403/404/400 will never succeed on retry and just burn the deadline. Fail straight to the deterministic path; keep retries for 429/5xx/timeouts.
- **Response-cache key includes `scenario_id`.** `_scenario_key` hashes the whole payload, so two scenarios identical except for `scenario_id` are cache misses. That's correct and safe — just confirming it's deliberate, since the opposite would echo the wrong `scenario_id` and fail the schema check.

---

## 3. What to port from my tree

Everything is in `~/hackathon_bup` (my clone of yours is at `…/scratchpad/mate`).

| Take | From | For |
|---|---|---|
| `allow_inf_nan=False` pattern | `app/schemas.py` | P0-1 |
| Diagnostics + degraded-run banner | `app/main.py`, `scripts/score_public.py` | P0-2 |
| Extra paraphrase cues + percent-of-capacity regex | `app/fallback.py` | P1-1 |
| 61-test pytest suite as a template | `tests/` | P1-2 |
| Provider abstraction + Anthropic provider | `app/llm/` | P1-3 |
| Shared 17-case cross-test | in this review | regression |

---

## 4. The one blocking item — neither of us has solved it

**Neither implementation has ever made a successful live LLM call.** Both 60/60 scores came from the deterministic path.

The Gemini key we have (`AQ.Ab8R…`, now revoked — it was pasted in a chat transcript) authenticates fine and lists 32 models, but **`generateContent` returns 403 "Your project has been denied access" on all 11 models I tried** — 3.8-flash, 3.7, 3.6, 3.5, 3.1-flash-lite, 3-flash-preview, flash-latest, pro-latest, 3.1-pro-preview, gemma-4-31b, and 2.5-flash. (`gemini-2.5-flash` and `-flash-lite`, which are `GEMINI_MODEL` and the first fallback in your chain, additionally return **404 "no longer available to new users"** — so update `MODEL_CHAIN` regardless of which key we end up with.)

The block is on the Google Cloud **project**, not the key, so generating another key in the same project won't help.

**Options, in order:**
1. Fresh AI Studio key in a **brand-new project** — free, fastest.
2. Anthropic credits — $5 minimum covers the whole round. Measured against our prompt (~856 in / ~260 out): Haiku 4.5 ≈ $1.08 per 500 requests, Sonnet 5 ≈ $3.23, Opus 5 ≈ $5.39. Requires P1-3 first.

**Whoever gets a working key, run this immediately and paste the output:**
```bash
python test_local.py --base-url http://localhost:8000
curl -s http://localhost:8000/diagnostics
```
We need `interpreted_by_llm: 10`, `interpreted_by_fallback: 0`. Until we see that, we are not eligible, regardless of the score.

---

## 5. Pre-submit checklist

- [ ] **P0-1** `Infinity` returns 400, with hostile-suite coverage
- [ ] **P0-2** `/diagnostics` live; harness exits non-zero on a degraded run
- [ ] **Working LLM key verified** — `interpreted_by_llm: 10`, no warning banner
- [ ] **P1-1** 17/17 on the shared paraphrase set
- [ ] `MODEL_CHAIN` updated — `gemini-2.5-flash` / `-flash-lite` are 404 for new keys
- [ ] Endpoint deployed, publicly reachable, `/health` + `/optimize-energy` from outside our network
- [ ] Render free tier: uptime pinger on `/health`, or upgrade — cold start blows the 30 s limit
- [ ] Docker image pushed, **exact tag/digest** in the README, verified `docker pull` + `docker run` from a clean machine
- [ ] No secrets in repo, image, logs, or responses — `git log -p | grep -iE 'ghp_|sk-ant|AIza|AQ\.'` comes back clean
- [ ] `public_cases.json` and the PDFs: confirm redistributing organizer material in a public repo is acceptable, or gitignore them
- [ ] Repo made public **after** the deadline
- [ ] 3-minute video: problem → architecture → LLM/guardrails/optimizer flow → how to run it (tie-break only, but it's the first tie-break)

---

## 6. Things I'd leave exactly as they are

Genuinely good calls, don't let refactoring erode them:

- **The arbiter.** Best idea in either codebase.
- **Minimal-subset relaxation** — strictly better than my fixed priority order.
- **Note-level normalized cache** — the real quota saver, and it makes interpretation deterministic across repeated notes.
- **Circuit breaker + key pool + model chain** — correct design for a free tier.
- **`run_in_executor` for the LP** — right call; HiGHS releases the GIL and this is what keeps the event loop free.
- **422 for semantic conflicts vs 400 for structural** — matches Problem Statement §6.1 more precisely than my blanket 400.
- **Swagger/ReDoc with the directive table in the description** — costs nothing and reads as polish to a human reviewer.
