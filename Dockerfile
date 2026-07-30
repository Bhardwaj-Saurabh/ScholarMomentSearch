# One image, four runnables (command picks which):
#   API   (default CMD)                    — presign + register + search + UI :8000
#   Worker (python -m src.worker)          — Prefect flow worker (user ingest)
#   CLIP  (uvicorn src.clip_service:app)   — one warm model behind a URL :8001
#   Seed  (python -m src.seed)             — one-shot sample gate, then exits
FROM python:3.11-slim

# ffmpeg = frame sampling. nodejs = the JavaScript runtime yt-dlp needs to
# extract YouTube formats (without it, EVERY YouTube video fails with "This
# video is not available"). Both matter only to the worker but cost little here.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
# CPU-only torch first: the default Linux wheel drags in ~6GB of CUDA libs
# that CLIP-on-CPU never uses.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY ui/ ui/
# src/samples.py._load_corpus() reads this at runtime (seed gate, component 10)
COPY benchmark/corpus.json benchmark/corpus.json

# Component 28: don't run any of the four runnables as root. `--create-home`
# (not --no-create-home) matters: HuggingFace/torch's model cache resolves
# against $HOME. A live docker-build smoke test (spec-guardian review) then
# caught a SECOND layer of this same problem: mounting a volume (hf_cache) or
# a fresh host bind mount (./data) at container START overwrites whatever
# this build-time chown did — the mount point's ownership wins, not the
# image's. entrypoint.sh fixes that at the only point it actually can: it
# runs as root, chowns the just-mounted paths, then drops to appuser before
# exec'ing the real command — the image itself never runs application code
# as root.
RUN useradd --create-home --uid 1000 appuser \
 && chown -R appuser:appuser /app
ENV HOME=/home/appuser
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
# Same convention as fly.toml's [[http_service.checks]] and docker-compose's
# api healthcheck — all three point at src/health.py's real dependency check
# via GET /api/health, not just "is the process alive."
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:8000/api/health', timeout=4)" || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "src.app:app", "--host", "0.0.0.0", "--port", "8000"]
