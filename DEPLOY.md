# Deployment Runbook — GridWise LLM

Every command, in order, from a fresh checkout to a judge-reachable public URL.
Target platform: **Render** (Docker web service), plus a Docker Hub fallback image.

Total time: ~25 minutes, most of it waiting on builds.

---

## Step 0 — Prerequisites

| Need | Where |
|---|---|
| Google Gemini API key | https://aistudio.google.com/apikey |
| GitHub account | repo must be **created after question reveal**, private during the event, public after the deadline |
| Render account | https://render.com — card added (the **free tier sleeps**, which breaks judging) |
| Docker Desktop | only for the fallback image; Render builds the image itself |
| `gh` CLI (optional) | makes the repo step one command |

---

## Step 1 — Local setup and verification

```bash
cd "C:/Hackathon/BUP Hackathon"

python -m venv .venv
source .venv/Scripts/activate        # Git Bash on Windows
# PowerShell:  .\.venv\Scripts\Activate.ps1
# macOS/Linux: source .venv/bin/activate

pip install -r requirements.txt
```

Set the key for this shell:

```bash
export GEMINI_API_KEY="paste-your-key-here"        # Git Bash / macOS / Linux
# PowerShell:  $env:GEMINI_API_KEY = "paste-your-key-here"
```

Or write it once to `.env` (already gitignored — **never commit this file**):

```bash
cp .env.example .env
# then edit .env and fill in GEMINI_API_KEY
```

### 1a. Solver check — no API key needed

```bash
python test_local.py --offline
```

Expected:

```
interpretation 10/10   valid 10/10   avg cost ratio 1.0000
```

### 1b. Start the service

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
curl http://localhost:8000/health
# {"status":"ok"}
```

### 1c. Full end-to-end check against the live model

```bash
python test_local.py --base-url http://localhost:8000
```

Target: `interpretation 10/10   valid 10/10   avg cost ratio 1.0000`.

**Do not deploy until 1c passes.** If interpretation fails on a case, the fix is in the
`SYSTEM_INSTRUCTION` prompt in `llm.py`, not in the optimizer.

### 1d. Error-path sanity

```bash
# malformed JSON -> 400
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/optimize-energy \
  -H 'Content-Type: application/json' -d '{"scenario_id": '

# a real sample case -> 200
python -c "import json;print(json.dumps(json.load(open('public_cases.json'))['cases'][0]['input']))" > /tmp/case1.json
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8000/optimize-energy \
  -H 'Content-Type: application/json' --data @/tmp/case1.json
```

Stop the server with `Ctrl+C`.

---

## Step 2 — Git repository

```bash
cd "C:/Hackathon/BUP Hackathon"

git init -b main
git add .
git status          # CONFIRM: .env is NOT listed
git commit -m "GridWise LLM: LLM-assisted operator directive interpretation + LP optimizer"
```

> **Stop and check `git status` output.** If `.env` appears, run `git rm --cached .env`
> before committing. A committed key fails the security rule in the participant guide.

Create the private repo and push:

```bash
# with the GitHub CLI
gh repo create gridwise-llm --private --source=. --remote=origin --push
```

Or manually — create an empty private repo on github.com, then:

```bash
git remote add origin https://github.com/<your-user>/gridwise-llm.git
git push -u origin main
```

---

## Step 3 — Deploy to Render

`render.yaml` is already in the repo, so use the Blueprint path.

### 3a. Create the service

1. Go to <https://dashboard.render.com> → **New** → **Blueprint**.
2. Connect your GitHub account and pick the `gridwise-llm` repo.
3. Render reads `render.yaml` and proposes one web service named `gridwise-llm`:
   - Runtime: **Docker**
   - Dockerfile path: `./Dockerfile`
   - Health check path: `/health`
   - Plan: **Starter**
4. It will prompt for `GEMINI_API_KEY` (it is declared `sync: false`, so it is never
   stored in the repo). Paste the key there.
5. Click **Apply** / **Create**.

**If the Blueprint flow gives trouble, use the manual path instead:**
**New** → **Web Service** → connect the repo → Language **Docker** → Instance type
**Starter** → Health Check Path `/health` → add environment variable
`GEMINI_API_KEY` → **Create Web Service**.

### 3b. Confirm the plan is not Free

In the service → **Settings** → **Instance Type**, confirm **Starter** (or higher).

Free instances sleep after ~15 minutes idle, and the cold start can exceed the judge's
30-second per-request limit and fail the health check. This is the single most common way
teams lose the deployment points.

### 3c. Watch the build

The **Logs** tab should end with:

```
Application startup complete.
Uvicorn running on http://0.0.0.0:10000
==> Your service is live 🎉
```

Render injects `PORT` (usually 10000); the Dockerfile already binds `0.0.0.0:$PORT`.

Your URL appears at the top of the service page:
`https://gridwise-llm-XXXX.onrender.com`

---

## Step 4 — Verify the deployment from outside

Replace `$URL` with your real Render URL.

```bash
export URL="https://gridwise-llm-XXXX.onrender.com"

curl "$URL/health"
# {"status":"ok"}

python test_local.py --base-url "$URL"
```

Target again: `interpretation 10/10   valid 10/10   avg cost ratio 1.0000`.

**Then test from a different network** — phone hotspot, or a teammate's machine. This
catches the case where the service is only reachable from your own session. The judge has
no VPN, no login, no dashboard access.

---

## Step 5 — Keep it warm during the judging window

Even on a paid instance, add an external pinger so `/health` is never the first request
after an idle stretch:

- <https://uptimerobot.com> or <https://cron-job.org>
- Monitor type HTTP(s), URL `https://<your-app>.onrender.com/health`, interval 5 minutes.

---

## Step 6 — Docker fallback image (required deliverable)

The submission requires a pullable image the organizers can run themselves.

```bash
cd "C:/Hackathon/BUP Hackathon"

docker login
docker build -t <dockerhub-user>/gridwise-llm:1.0.0 .
```

Test it locally **before** pushing:

```bash
docker run --rm -p 8000:8000 -e GEMINI_API_KEY="$GEMINI_API_KEY" \
  <dockerhub-user>/gridwise-llm:1.0.0

# in another terminal
curl http://localhost:8000/health
python test_local.py --base-url http://localhost:8000
```

Push and record the exact digest:

```bash
docker push <dockerhub-user>/gridwise-llm:1.0.0
docker inspect --format='{{index .RepoDigests 0}}' <dockerhub-user>/gridwise-llm:1.0.0
```

Make the Docker Hub repository **public** so judges can pull it, and confirm the image
contains no secrets:

```bash
docker run --rm <dockerhub-user>/gridwise-llm:1.0.0 sh -c 'env | grep -i gemini || echo "no key baked in"'
```

Put the exact tag/digest and this run command in `README.md`:

```bash
docker pull <dockerhub-user>/gridwise-llm:1.0.0
docker run -p 8000:8000 -e GEMINI_API_KEY=<key> <dockerhub-user>/gridwise-llm:1.0.0
```

---

## Step 7 — Redeploying after a code change

```bash
git add -A
git commit -m "tune interpretation prompt"
git push
```

Render auto-deploys on push to `main`. Watch Logs until "service is live", then re-run:

```bash
curl "$URL/health" && python test_local.py --base-url "$URL"
```

Rebuild and push the Docker image too if the change is going into the final submission.

---

## Step 8 — Before the submission deadline

```bash
# make the repo public (required for evaluation)
gh repo edit <your-user>/gridwise-llm --visibility public --accept-visibility-change-consequences
```

Then confirm, one by one:

- [ ] `GET /health` returns `{"status":"ok"}` from an outside network
- [ ] `POST /optimize-energy` accepts a public sample case and returns 200
- [ ] `python test_local.py --base-url $URL` → 10/10 interpretation, 10/10 valid, ratio 1.0000
- [ ] Malformed JSON returns 400, not 500
- [ ] Repo is public, and contains **no** `.env` and no key in any file
- [ ] `README.md` has the live URL, the exact Docker tag, and the env-var names
- [ ] Docker image pulls and runs from a clean machine
- [ ] Uptime pinger is running
- [ ] 3-minute video link is accessible (tie-break only)

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Render build fails on `pip install scipy` | memory limit on a tiny instance | use Starter or higher; scipy ships wheels, so it should not compile |
| Service builds but health check fails | not binding `$PORT` or `0.0.0.0` | the Dockerfile `CMD` already handles this — confirm you did not override the start command in the dashboard |
| All notes come back `no_op` in production | `GEMINI_API_KEY` missing on Render | Environment tab → add the variable → Manual Deploy |
| Interpretation works locally, fails deployed | different `GEMINI_MODEL` value | set `GEMINI_MODEL` explicitly on Render |
| Latency spikes to 20–30 s | free instance cold start | move to Starter and add the pinger (Step 5) |
| 429 from Gemini under repeated judging | free-tier model quota | enable billing on the Google Cloud project behind the key |
| `/health` fine, `/optimize-energy` returns 500 | look at Render Logs; the handler logs the traceback server-side while returning a clean `{"error":"internal error"}` to the caller | |

---

## Quick reference

```bash
# local
uvicorn main:app --host 0.0.0.0 --port 8000
python test_local.py --offline
python test_local.py --base-url http://localhost:8000

# deployed
curl "$URL/health"
python test_local.py --base-url "$URL"

# docker
docker build -t <user>/gridwise-llm:1.0.0 .
docker run --rm -p 8000:8000 -e GEMINI_API_KEY=<key> <user>/gridwise-llm:1.0.0
docker push <user>/gridwise-llm:1.0.0
```
