# Deployment & Verification Runbook — GridWise LLM

Everything needed to go from a clean machine to a judged public endpoint, and every test
to run before you submit. Commands are given for PowerShell and bash.

**Order matters.** Deploy the skeleton early (Step 4). Hunting deploy bugs at 3:50 into a
4-hour window is how teams lose the 10 deployment points.

---

## Step 0 — Prerequisites

| Need | Where |
|---|---|
| Python 3.11+ | <https://www.python.org/downloads/> |
| Docker Desktop | <https://www.docker.com/products/docker-desktop/> |
| A **free** Gemini API key | <https://aistudio.google.com/apikey> |
| Render account | <https://render.com> (free plan is fine — see Step 6) |
| Docker Hub account | <https://hub.docker.com> (for the fallback image) |

### About the free Gemini tier

The free Google AI Studio key is enough for this round, but know its shape:

**Measured against a live free key on this build:** `gemini-2.5-flash` returns

```
429 RESOURCE_EXHAUSTED ... Quota exceeded for metric:
generativelanguage.googleapis.com/generate_content_free_tier_requests,
limit: 20, model: gemini-2.5-flash. Please retry in 25.8s
```

So the binding constraint is **20 requests per minute, per model, per project** — a
*per-minute* cap, not your daily allowance. A burst of ten scenarios back to back will hit
it; requests spaced a few seconds apart will not.

Models were probed against that same key. These are **dead for newly created keys** and
must not be in the chain: `gemini-2.5-flash-lite`, `gemini-2.0-flash`, `gemini-2.5-pro`
(all 404). These work: `gemini-2.5-flash`, `gemini-flash-latest`, `gemini-3.5-flash`,
`gemini-3.1-flash-lite`. `gemini-3.5-flash-lite` works only without a thinking budget —
the service detects that and retries without the field automatically.

The service is built for all of this:

- `LLM_MAX_CONCURRENCY=4` keeps in-flight calls under the RPM ceiling.
- A 429 rotates the key **and** steps to the next model in `GEMINI_FALLBACK_MODELS`.
- The normalized note cache means a repeated note never reaches the provider again.
- If everything is exhausted, the deterministic extractor answers and the service keeps
  returning valid 200s.

**Strongly recommended — this is the single highest-value change you can make.** The quota
is per *project*, so create 2–3 more free keys in **different Google Cloud projects** and
set them as a pool. Three keys turn 20 RPM into 60 RPM for the cost of one env var:

```
GEMINI_API_KEYS=key_one,key_two,key_three
```

With one key, a ten-scenario burst exhausts the minute and the service answers the rest
from the deterministic reading — correct, but it forfeits the "LLM produced this" path on
those cases and adds latency while the chain is walked. With three keys that does not
happen.

---

## Step 1 — Local setup

```powershell
# PowerShell
cd "C:\Hackathon\BUP Hackathon _2"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:GEMINI_API_KEY = "paste-your-free-key-here"
```

```bash
# bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
export GEMINI_API_KEY="paste-your-free-key-here"
```

Never put the key in a file that git tracks. `.gitignore` already covers `.env`, `*.key`
and `*.pem`; `.env.example` holds names only.

---

## Step 2 — Local verification (run all of these)

### 2a. Solver check — no API key needed

```bash
python test_local.py --offline
```

**Expect:** `interpretation 10/10   valid 10/10   avg cost ratio 1.0000`, p95 a few ms.
Any cost above the reference means the LP lost optimality — stop and fix before anything
else, because optimization credit is only scored on cases that are already valid.

### 2b. Paraphrase suite — the hidden-set proxy

```bash
python test_paraphrase.py            # deterministic extractor
python test_paraphrase.py --llm      # live model + guardrails (uses ~43 free calls)
```

**Expect:** `43/43`. Hidden notes reword the same directives, so this is the closest
local proxy for the 5 paraphrase-robustness points. Run the `--llm` form **once** — it
spends real free-tier quota.

### 2c. Start the service

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}
```

Open <http://localhost:8000/docs>. You should see both endpoints, the request example,
and 200 / 400 / 422 / 500 documented on `POST /optimize-energy`. ReDoc is at `/redoc`.

### 2d. Full pipeline against the running service

```bash
python test_local.py --base-url http://localhost:8000
python test_paraphrase.py --base-url http://localhost:8000
```

**Expect:** 10/10 and 43/43. This is the whole path — LLM, guardrails, LP, replay.

### 2e. Hostile input drill

```bash
python test_hostile.py --base-url http://localhost:8000
```

**Expect:** `29 passed, 0 failed`. Truncated JSON, wrong types, 0 or 4 notes, 23 or 25
hours, duplicate hours, negatives, NaN, null battery → controlled `400`; contradictory
battery numbers → `422`; huge-but-valid and all-zero scenarios → `200`. The suite also
scans every response body for leaked keys, prompts and stack traces.

### 2f. Provider-failure drill — do not skip this

Point the service at a key that cannot work, restart it, and re-run 2d:

```powershell
$env:GEMINI_API_KEY = "deliberately-invalid-key"
uvicorn main:app --port 8000
```

**Expect:** still `10/10` valid and `43/43` paraphrases, via the deterministic reading.
This is what protects you if the free-tier quota runs out mid-judging. Verified on this
build. Restore the real key afterwards.

### 2g. Latency and stability

```bash
python -c "
import json,time,urllib.request
case=json.load(open('public_cases.json'))['cases'][0]['input']
lat=[]
for i in range(20):
    b=json.dumps(dict(case, scenario_id=f'LAT-{i}')).encode()
    r=urllib.request.Request('http://localhost:8000/optimize-energy',data=b,
                             headers={'Content-Type':'application/json'})
    t=time.perf_counter(); urllib.request.urlopen(r,timeout=30).read()
    lat.append(time.perf_counter()-t)
lat.sort(); print(f'p50 {lat[9]*1000:.0f} ms  p95 {lat[18]*1000:.0f} ms  max {lat[-1]*1000:.0f} ms')"
```

**Target:** p95 ≤ 5 s → full 3 latency points. Between 5 s and 15 s scores 2/3; above 30 s
counts as a failure. Distinct `scenario_id`s defeat the response cache so this measures
real work. Note the free tier is the usual cause of a slow tail — the note cache removes
it for repeated wording.

---

## Step 3 — Repository

Per the rulebook: create the repo **after** the question reveal, keep it **private**
during the event, make it **public after** the submission deadline.

```bash
git status                 # confirm .env is NOT listed
git add .
git commit -m "GridWise LLM solution"
git push origin main
```

Before pushing, confirm no secrets are tracked:

```bash
# Google AI Studio keys appear as either AIza... or AQ.Ab8... -- check for both.
git grep -nE "AIza[0-9A-Za-z_-]{20,}|AQ\.[A-Za-z0-9_-]{20,}" -- .
# expect: no output
```

---

## Step 4 — Deploy to Render

### 4a. Create the service

1. Render Dashboard → **New** → **Web Service** → connect the GitHub repo.
2. Language / runtime: **Docker**. Render picks up the `Dockerfile` automatically.
3. Instance type: **Free** (see the cold-start warning below) or **Starter** for always-on.
4. **Health Check Path:** `/health` — set this, it is how Render knows a deploy is live.
5. Environment variables (dashboard only, never in the repo or image):

   | Key | Value |
   |---|---|
   | `GEMINI_API_KEY` | your free key |
   | `GEMINI_API_KEYS` | optional pool, comma-separated |
   | `GEMINI_MODEL` | `gemini-2.5-flash` |
   | `GEMINI_FALLBACK_MODELS` | `gemini-2.5-flash-lite,gemini-2.0-flash` |
   | `LLM_MAX_CONCURRENCY` | `4` |
   | `REQUEST_BUDGET_S` | `25` |
   | `WEB_CONCURRENCY` | `2` |

   Do **not** set `PORT` — Render injects it, and the Dockerfile already honours it.

6. Create the service and watch the build log until it reports **Live**.

`render.yaml` in the repo encodes all of the above if you prefer a Blueprint deploy.

### 4b. Verify from outside your machine

```bash
curl -s https://<your-service>.onrender.com/health
python test_local.py --base-url https://<your-service>.onrender.com
```

Then repeat the health check **from a different network** — a phone hotspot is enough.
The judge must reach it with no login, no VPN, no manual approval. First-call latency
after a deploy includes a container start; run the check twice.

---

## Step 5 — Keep it warm (free plan)

> **Cold-start warning.** A Render free instance sleeps after ~15 minutes idle, and the
> first request after that can take 30–50 s — past the judge's 30 s per-request limit, and
> it can fail the `/health` readiness check too. That risks 3 health + 3 latency +
> 3 reachability points.

Set up an external pinger before judging starts:

1. Go to <https://cron-job.org> (or UptimeRobot).
2. Create a job: `GET https://<your-service>.onrender.com/health`.
3. Interval: **every 5 minutes**.
4. Enable it from before 7:00 PM until after 11:00 PM and confirm it is reporting 200s.

`/health` is a static literal that touches neither the model nor the solver, so pinging it
costs no quota.

Belt and braces: keep a browser tab on `/health` and refresh it occasionally during the
window.

---

## Step 6 — Docker fallback image (required deliverable)

The judge must be able to pull and run your image as a fallback path.

```bash
docker build -t <dockerhub-user>/gridwise-llm:1.0.0 .

# verify locally BEFORE pushing
docker run --rm -p 8000:8000 -e GEMINI_API_KEY="your-free-key" \
       <dockerhub-user>/gridwise-llm:1.0.0
# second terminal:
curl -s http://localhost:8000/health
python test_local.py --base-url http://localhost:8000

docker login
docker push <dockerhub-user>/gridwise-llm:1.0.0
docker inspect --format='{{index .RepoDigests 0}}' <dockerhub-user>/gridwise-llm:1.0.0
```

Make the Docker Hub repository **public** and submit the exact tag or digest.

Confirm no secret got baked in:

```bash
docker run --rm <dockerhub-user>/gridwise-llm:1.0.0 sh -c 'env | grep -i -E "gemini|api" || echo CLEAN'
# expect: CLEAN
```

**Clean-machine drill** — exactly what the judge does. On a machine that has never built
this project:

```bash
docker pull <dockerhub-user>/gridwise-llm:1.0.0
docker run --rm -p 8000:8000 -e GEMINI_API_KEY="your-free-key" <dockerhub-user>/gridwise-llm:1.0.0
curl -s http://localhost:8000/health
```

Submit that exact `docker pull` + `docker run` pair in the README.

---

## Step 7 — Redeploying after a change

```bash
git add . && git commit -m "…" && git push origin main
```

Render auto-deploys from `main`. After **Live**, re-run:

```bash
curl -s https://<your-service>.onrender.com/health
python test_local.py --base-url https://<your-service>.onrender.com
```

Rebuild and push the Docker image too if the code changed, so the fallback matches.

---

## Step 8 — Before the deadline

```bash
# make the repo public (required for evaluation)
gh repo edit <user>/<repo> --visibility public --accept-visibility-change-consequences
```

Keep the endpoint, the image and the video link reachable through the whole judging window.

### Final pre-submit checklist

- [ ] `GET /health` returns `{"status":"ok"}` from an outside network.
- [ ] `POST /optimize-energy` accepts 1–3 notes with the exact request schema.
- [ ] Exactly one `directive_interpretation` entry per note, in `note_index` order;
      `no_op` uses `applies = false` + null adjustment; all others `applies = true`.
- [ ] Directive hours are unique integers 0–23 ascending; invalid model output cannot
      invent a constraint.
- [ ] `hourly_plan` obeys the ground-truth directives plus energy balance, effective
      solar, battery bounds, rate limits, grid caps and end-of-day neutrality.
- [ ] `total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh` match a recalculation from
      `hourly_plan`.
- [ ] README has a clean quickstart, env var **names**, model/provider, guardrails,
      solver, run command, curl examples, sample test, dependencies, limitations,
      and **no committed secrets**.
- [ ] Repo created after reveal, private during, public after the deadline.
- [ ] Docker image pullable at an exact tag/digest; documented run command reaches
      `/health`; no baked credentials.
- [ ] ≤ 3-minute video explaining problem → architecture → LLM → guardrails → optimizer
      → how it is run and tested.
- [ ] Uptime pinger running for the whole window.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| All notes come back `no_op` | No key, or every model exhausted | Check `GEMINI_API_KEY` in the dashboard; look for `quota/rate limit` in the logs. The rule path should already be covering you — verify with `test_paraphrase.py --base-url`. |
| `429` / `RESOURCE_EXHAUSTED` in logs | Free-tier RPM or daily cap | Add more keys to `GEMINI_API_KEYS`; lower `LLM_MAX_CONCURRENCY`; the model chain already steps down automatically. |
| First request after idle takes 40 s | Free-instance cold start | The uptime pinger in Step 5. This is the single biggest free-plan risk. |
| Render build fails | Dockerfile or deps | Read the build log; reproduce with `docker build -t t .` locally. |
| `/health` fine, `/optimize-energy` 500s | Unhandled path | Check logs for `"event":"unhandled"`; the body never leaks details by design. |
| p95 above 5 s | Model latency | Confirm `LLM_MAX_CONCURRENCY=4`, `LLM_TIMEOUT_S=12`; repeated notes should hit the cache and return in milliseconds. |
| Plan rejected by the judge | Replay caught it first? | Search logs for `replay_rejected` / `fallback`. Reproduce locally with `test_local.py --base-url`. |

---

## Quick reference

```bash
# local
uvicorn main:app --host 0.0.0.0 --port 8000
curl -s http://localhost:8000/health
python test_local.py --offline
python test_local.py --base-url http://localhost:8000
python test_paraphrase.py --base-url http://localhost:8000
python test_hostile.py  --base-url http://localhost:8000

# deployed
curl -s https://<your-service>.onrender.com/health
python test_local.py --base-url https://<your-service>.onrender.com

# docker
docker build -t <user>/gridwise-llm:1.0.0 .
docker run --rm -p 8000:8000 -e GEMINI_API_KEY="…" <user>/gridwise-llm:1.0.0
docker push <user>/gridwise-llm:1.0.0
```
