"""Component 28 + 41 (DESIGN.md §3e) — deploy/CI config, verified as data.

`fly.toml`, `Dockerfile`, `docker-compose.yml`, and the GitHub Actions
workflows are configuration, not code paths pytest normally exercises — but a
silently-wrong deploy config is exactly how this repo ended up with a health
check nothing probed and a deploy workflow pointed at a branch that doesn't
exist. These parse the real files and assert the shape, so a future edit that
breaks the wiring fails CI instead of failing silently in production.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_fly_toml_has_a_real_http_health_check():
    data = tomllib.loads((ROOT / "fly.toml").read_text())
    checks = data["http_service"]["checks"]
    assert checks, "fly.toml must declare at least one http_service check"
    paths = [c.get("path") for c in checks]
    assert "/api/health" in paths


def test_dockerfile_has_a_healthcheck_instruction():
    text = (ROOT / "Dockerfile").read_text()
    assert "HEALTHCHECK" in text
    assert "/api/health" in text


def test_dockerfile_runs_as_non_root_user():
    text = (ROOT / "Dockerfile").read_text()
    lines = [l.strip() for l in text.splitlines() if l.strip().startswith("USER ")]
    assert lines, "Dockerfile must switch to a non-root USER before CMD"
    assert lines[-1] != "USER root"


def test_compose_api_service_has_a_healthcheck():
    data = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
    api = data["services"]["api"]
    assert "healthcheck" in api, "the api service should declare a healthcheck:"
    assert "/api/health" in str(api["healthcheck"].get("test", ""))


def test_deploy_workflow_targets_a_real_branch():
    data = yaml.safe_load((ROOT / ".github/workflows/fly-deploy.yml").read_text())
    real_branches = {"main"}
    # PyYAML parses the bare `on:` key as boolean True in YAML 1.1 - handle both.
    on_block = data.get("on", data.get(True))
    triggers = on_block if isinstance(on_block, dict) else {}
    workflow_run = triggers.get("workflow_run")
    assert workflow_run, "deploy should trigger off the CI workflow completing, not push directly"
    assert set(workflow_run.get("branches", [])) & real_branches, (
        "deploy workflow must key off a branch that actually exists"
    )


def test_ci_workflow_exists_and_runs_pytest_on_main():
    path = ROOT / ".github/workflows/ci.yml"
    assert path.exists(), "component 41 requires a CI test workflow"
    data = yaml.safe_load(path.read_text())
    on_block = data.get("on", data.get(True))
    assert "main" in on_block.get("push", {}).get("branches", [])
    assert "pull_request" in on_block

    steps_text = yaml.dump(data)
    assert "pytest" in steps_text
    assert "ruff" in steps_text
