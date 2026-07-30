#!/bin/sh
# Component 28: a Docker named volume (hf_cache) or a fresh host bind mount
# (./data) is root-owned at container START time regardless of what the
# Dockerfile's `chown` did at BUILD time — mounting replaces whatever was at
# that path, ownership included. A live docker-build smoke test (spec-guardian
# finding) caught this: the model cache was unwritable by the non-root
# `appuser` the moment a real volume was attached. Fixing it here, at
# container start (as root, before dropping to appuser), is what actually
# persists — build-time chown alone does not. Idempotent and cheap on an
# already-correct, warm cache.
set -e
chown -R appuser:appuser /home/appuser/.cache 2>/dev/null || true
[ -d /app/data ] && chown -R appuser:appuser /app/data 2>/dev/null || true
exec runuser -u appuser -- "$@"
