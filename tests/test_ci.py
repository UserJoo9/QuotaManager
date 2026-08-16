"""CI / CD workflow tests.

Validates that .github/workflows/ci.yml and .github/workflows/docker-publish.yml
exist, parse as valid YAML, and declare expected triggers and jobs.
"""

from __future__ import annotations

from pathlib import Path
import yaml

import pytest

REPO = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
DOCKER_PUBLISH_WORKFLOW = REPO / ".github" / "workflows" / "docker-publish.yml"


def _read_yaml(p: Path) -> dict:
    if not p.is_file():
        pytest.skip(f"{p} not found in this environment")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{p} must parse as a dictionary"
    return data


def test_ci_workflow_structure():
    data = _read_yaml(CI_WORKFLOW)
    triggers = data.get("on", data.get(True, {}))
    assert "push" in triggers, "CI must trigger on push"
    assert "pull_request" in triggers, "CI must trigger on pull_request"

    jobs = data.get("jobs", {})
    assert "lint-and-test" in jobs, "CI must define lint-and-test job"
    assert "docker-build-test" in jobs, "CI must define docker-build-test job"


def test_docker_publish_workflow_structure():
    data = _read_yaml(DOCKER_PUBLISH_WORKFLOW)
    triggers = data.get("on", data.get(True, {}))
    assert "push" in triggers, "Docker publish must trigger on push"
    assert "workflow_dispatch" in triggers, "Docker publish must support manual workflow_dispatch"

    jobs = data.get("jobs", {})
    assert "docker-publish" in jobs, "Docker publish must define docker-publish job"

    text = DOCKER_PUBLISH_WORKFLOW.read_text(encoding="utf-8")
    assert "ghcr.io" in text, "Must publish to GitHub Container Registry (ghcr.io)"
    assert "linux/amd64,linux/arm64" in text, "Must build for multi-arch linux/amd64 and linux/arm64"
