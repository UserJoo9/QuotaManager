"""Release workflow contracts that prevent publishing without same-run gates."""

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
WORKFLOWS = REPO / ".github" / "workflows"


def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def _triggers(workflow: dict) -> dict:
    # PyYAML's YAML 1.1 resolver parses the unquoted GitHub key `on` as True.
    return workflow.get("on", workflow.get(True, {}))


def test_release_waits_for_ci_and_packaging_smoke_in_same_tag_run():
    release = _workflow("release.yml")
    triggers = _triggers(release)
    assert triggers.get("push", {}).get("tags") == ["v*"]

    jobs = release["jobs"]
    gates = {name: job for name, job in jobs.items() if job.get("uses")}
    assert {job["uses"] for job in gates.values()} >= {
        "./.github/workflows/ci.yml",
        "./.github/workflows/packaging-smoke.yml",
    }
    build_steps = jobs["build"]["steps"]
    publish_steps = jobs["publish"]["steps"]
    upload = next(step for step in build_steps if step.get("uses") == "actions/upload-artifact@v4")
    download = next(step for step in publish_steps if step.get("uses") == "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093")
    assert upload["with"]["name"] == download["with"]["name"] == "release-assets"
    assert "softprops/action-gh-release@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65" in {
        step.get("uses") for step in publish_steps
    }

    publishing = jobs["publish"]
    assert set(publishing["needs"]) >= set(gates)
    assert publishing.get("permissions") == {"contents": "write"}
    assert release.get("permissions") == {"contents": "read"}


def test_ci_is_reusable_with_read_only_permissions():
    ci = _workflow("ci.yml")
    assert "workflow_call" in _triggers(ci)
    assert ci.get("permissions") == {"contents": "read"}


def test_packaging_smoke_covers_runtime_and_dependency_changes():
    smoke = _workflow("packaging-smoke.yml")
    triggers = _triggers(smoke)
    expected_paths = {
        "core/**",
        "quota/**",
        "api/**",
        "web/**",
        "run.py",
        "bin/**",
        "requirements-linux.txt",
        "LICENSE",
    }
    for event in ("push", "pull_request"):
        paths = set(triggers[event]["paths"])
        assert expected_paths <= paths
    assert "workflow_call" in triggers
    assert smoke.get("permissions") == {"contents": "read"}
