# Deploying MomentSearch to Fly.io

MomentSearch ships as **one Docker image** that runs as **three long-running
process groups** (`api`, `worker`, `clip`) plus a **one-shot seed gate** — each
on its own Fly machine, each scaled by its own bottleneck. This is the whole
point of the architecture: the pieces scale in different directions, so they
live on different machines.

```
                 ┌────────────── one image, three process groups ──────────────┐
 users ──HTTPS──►│  api      (:8000, public)   presign · register · search · UI │
                 │  worker   (no ports)        pulls ingest runs from Prefect    │
                 │  clip     (:8001, internal) ONE warm CLIP model behind a URL  │
                 └───────┬──────────────┬───────────────────┬───────────────────┘
                         ▼              ▼                    ▼
                   Neon Postgres   Prefect Cloud        Qdrant Cloud
                   (manifest)      (work queue)         (vectors)
                         ▲              ▲                    ▲
                         └───────  GCS bucket (videos + frame thumbnails)  ──────┘
```

## Why three separate services (not one box)

| Service | Scales on | Machine | Why separate |
|---|---|---|---|
| **api** | request concurrency | tiny, auto-stops when idle | stateless HTTP; must answer `202` instantly and never block on heavy work |
| **worker** | ingest throughput | cheap CPU, scale to N | download + ffmpeg per video; add replicas for a backfill, remove them after |
| **clip** | embedding FLOPs | one warm model (→ GPU later) | loading CLIP costs ~15–30s; doing it once in a shared service, not per-video, is the difference between fast and unusable |

If these were one process, you'd pay for a GPU on every web box, or reload the
model on every video, or block uploads behind embedding. Splitting them lets
each grow (and cost) independently: `fly scale count worker=5` for a big import,
or point `CLIP_SERVICE_URL` at a GPU machine when embedding is the wall — with
**zero code changes**.

Everything stateful is a rented managed service (Neon, Prefect Cloud, Qdrant
Cloud, GCS), so every Fly machine is disposable — "nothing on local."

## Prerequisites

You already have these wired in `.env` (they're external, so the same accounts
work from Fly):

- **Neon Postgres** — `DATABASE_URL`
- **Prefect Cloud** — `PREFECT_API_URL`, `PREFECT_API_KEY`
- **Qdrant Cloud** — `QDRANT_URL`, `QDRANT_API_KEY`
- **Object storage** — `STORAGE_PROVIDER=gcp_native` + the `GOOGLE_CLOUD_*` keys
  (bucket `momentsearch-media`)
- **LLM** — `LLM_API_KEY`
- A **Fly.io account** + the `flyctl` CLI installed.

> **The sample corpus is already indexed** in your shared Qdrant/Neon from local
> runs, so the deploy's seed gate finds them done and skips re-downloading — the
> deploy won't be blocked by YouTube.

## Deploy — step by step

All commands assume you're in the repo root. On Windows use PowerShell.

### 1. Authenticate

`flyctl` reads the `FLY_API_TOKEN` env var. The token lives in `.env` as
`FLY_IO_TOKEN` — load it into the session (this also works headless/CI, no
browser login needed):

```powershell
$env:FLY_API_TOKEN = ((Select-String '^FLY_IO_TOKEN=' .env).Line -replace '^FLY_IO_TOKEN=','').Trim().Trim('"')
fly auth whoami        # confirm it's your account
```

Bash equivalent:

```bash
export FLY_API_TOKEN="$(grep '^FLY_IO_TOKEN=' .env | cut -d= -f2- | tr -d '\r\"')"
fly auth whoami
```

### 2. Create the app (once)

```powershell
fly apps create momentsearch --org personal
```

If the name is taken, pick another (e.g. `momentsearch-<you>`) and update **two
places** in `fly.toml`: the `app = '…'` line and the `CLIP_SERVICE_URL`
internal-DNS host (`clip.process.<app-name>.internal`).

### 3. Push secrets (once, and whenever they change)

Set an **explicit, named list** of secrets. Do not bulk-import `.env`.

> This used to be a `Get-Content .env | fly secrets import` one-liner. It was
> replaced (DESIGN.md §3e component 27) because bulk import ships whatever the
> file happens to contain — including, at the time, the published default
> `ADMIN_TOKEN=change-me`, straight into production. Naming each secret means
> a placeholder can't ride along unnoticed, and it keeps local-only values
> (`FLY_*`, `YT_COOKIES_FILE`, a localhost `REDIS_URL`) out of the deploy.

First, make sure `ADMIN_TOKEN` is a real generated value, not a placeholder —
`fly.toml` sets `ENV = 'production'`, so the API **fails closed with a 503** on
every protected route if this is missing (that is deliberate: the alternative
is silently serving them to anyone):

```powershell
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

```powershell
fly secrets set `
  DATABASE_URL="…" `
  ADMIN_TOKEN="…" `                # generated above — never a placeholder
  QDRANT_URL="…" QDRANT_API_KEY="…" `
  PREFECT_API_URL="…" PREFECT_API_KEY="…" `
  STORAGE_PROVIDER="…" STORAGE_BUCKET="…" `
  STORAGE_ACCESS_KEY_ID="…" STORAGE_SECRET_ACCESS_KEY="…" `
  LLM_PROVIDER="…" LLM_API_KEY="…" LLM_MODEL="…" `
  REDIS_URL="…"                    # REQUIRED for a public deploy — see below

# YouTube cookies as a secret (needed because Fly's datacenter IP is bot-checked;
# there's no ./data mount on Fly, so the local file path won't work there —
# the worker decodes this to a temp file at runtime)
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes("data/cookies.txt"))
fly secrets set YT_COOKIES_B64="$b64"
```

Verify nothing placeholder-shaped made it through before deploying:

```powershell
fly secrets list          # names + digests only; values are never shown back
```

> **`REDIS_URL` is not optional for a public deploy.** Rate limiting
> (DESIGN.md §3e component 26) rides Redis and *fails open* by design — so
> with no `REDIS_URL`, throttling silently disappears from `/api/ask` and
> `/ask_stream`, which need no credentials and cost real LLM money per call.
> Caching degrades gracefully without Redis; abuse protection does not.
>
> **Proxy headers:** `fly.toml` sets `ENV = 'production'`, which turns on
> `TRUST_PROXY_HEADERS` so the limiter sees the real caller via
> `Fly-Client-IP` instead of putting every visitor in one shared bucket. If
> you deploy this image somewhere that is NOT behind a trusted proxy, set
> `TRUST_PROXY_HEADERS=false` — otherwise `X-Forwarded-For` is
> client-controlled and rotating it defeats the limiter.

### 4. Deploy

```powershell
fly deploy --ha=false
```

> **If the build fails with a 403** — e.g. `error building: ... (status 403):
> Your account has been marked as high risk`, or the remote builder is otherwise
> refused/unavailable — build the image **locally** instead (needs Docker Desktop
> running) so it never touches Fly's remote builder:
>
> ```powershell
> fly deploy --ha=false --local-only
> ```
>
> This builds with your local Docker daemon and pushes the finished image to
> `registry.fly.io`. Alternatively, verify the account at
> <https://fly.io/high-risk-unlock> to use the remote builder.

On deploy, fly.toml's `release_command` runs the **seed gate** first
(`python -m src.seed`). Because the samples are already indexed in your shared
Qdrant/Neon, it exits in seconds and the app goes live. If it can't verify the
samples it aborts and the previous version keeps serving — you never get a
half-indexed app.

### 5. Open it

```powershell
fly open           # -> https://momentsearch.fly.dev/
fly logs           # tail all processes
```

## Scaling knobs

```powershell
fly scale count worker=3          # more ingest throughput (concurrent videos)
fly scale count worker=0 clip=0   # between ingest sessions — queued runs just wait
fly secrets set WORKER_CONCURRENCY=3   # more videos per worker machine
```

The `api` machine auto-stops when idle and auto-starts on the next request
(`min_machines_running = 0` in fly.toml), so it costs almost nothing at rest.

## CI/CD (optional)

`.github/workflows/fly-deploy.yml` redeploys automatically on **every push to
`dev`** (it runs `flyctl deploy --remote-only`). One-time setup — add a deploy
token as the `FLY_API_TOKEN` repo secret:

```powershell
fly tokens create deploy -x 999999h
# GitHub → Settings → Secrets and variables → Actions → New repository secret
```

> CI uses Fly's **remote** builder, so if the account is flagged "high risk"
> (see Troubleshooting) CI deploys fail there too — unlock the account, or deploy
> manually with `fly deploy --local-only` from a machine that has Docker until
> it's cleared.

## Cost (rough)

| Piece | At rest | Active |
|---|---|---|
| api (auto-stop) | ~$0 | ~$2–6/mo |
| worker | scale to 0 between sessions | ~$2–5/mo up |
| clip | scale to 0 between sessions | ~$2–5/mo (CPU) |
| Neon / Prefect / Qdrant | free tiers | — |
| GCS | ~$1–2/mo per 50 GB | — |
| LLM | — | ~$0.005–0.01 per question |

Everything-on ≈ **$40/mo**; idle-scaled with free tiers ≈ **$5–10/mo**. GPU (for
the clip service) is a burst cost only — rent it for a big backfill, kill it after.

## Troubleshooting

- **Build fails with 403 / "high risk account" / remote builder error** → Fly's
  shared remote builder refused the build. Build locally instead:
  `fly deploy --ha=false --local-only` (needs Docker Desktop running), or unlock
  the account at <https://fly.io/high-risk-unlock>. This is a builder/account
  issue, not a code issue — the same image builds fine locally.
- **Deploy aborts on release_command** → the seed gate couldn't verify samples.
  Check `fly logs`; usually a bad `DATABASE_URL`/`QDRANT_URL` secret. Set
  `SEED_SAMPLE_VIDEOS=false` to skip the gate if you need to deploy anyway.
- **YouTube ingest fails on Fly** → datacenter IP is blocked; make sure
  `YT_COOKIES_B64` is set (step 3). Cookies expire in ~2–3 weeks; re-run the
  `fly secrets set YT_COOKIES_B64=…` command to refresh. Uploads are unaffected.
- **Browser uploads fail** → the GCS bucket needs a CORS rule allowing `PUT`
  from your site's origin (see `.env.example`).
- **`clip` unreachable** → confirm `CLIP_SERVICE_URL` in fly.toml matches the
  app name (`clip.process.<app>.internal:8001`).
