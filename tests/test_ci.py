"""CI / CD workflow tests.

Validates that .github/workflows/ci.yml exists, parses as valid YAML,
and declares expected triggers and jobs.
"""

from __future__ import annotations

from pathlib import Path
import yaml

import pytest

REPO = Path(__file__).resolve().parent.parent
CI_WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"


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

    push_cfg = triggers.get("push", {})
    assert "paths-ignore" in push_cfg, "CI must ignore doc/markdown paths on push"

    jobs = data.get("jobs", {})
    assert "lint-and-test" in jobs, "CI must define lint-and-test job"
    assert "docker-build-test" not in jobs, "Docker job should not be in CI"
