"""Executable guards and publication policy for the Rust package."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def _workflow(name):
    return yaml.load(
        (ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader
    )


def _step(name):
    return next(
        s
        for s in _workflow("publish.yml")["jobs"]["build"]["steps"]
        if s.get("name") == name
    )


def test_package_metadata_requires_published_collection_api_and_shared_build_tools():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = config["project"]
    assert project["name"] == "maid-validator-rust"
    assert project["version"] == "0.1.0"
    assert "maid-runner>=2.27.6,<3" in project["dependencies"]
    assert config["build-system"]["requires"] == ["setuptools==83.0.0", "wheel==0.47.0"]
    assert project["entry-points"]["maid_runner.validators"] == {
        "rust": "maid_validator_rust:RustValidator"
    }
    assert project["license"] == "MIT" and project["license-files"] == ["LICENSE"]
    for version in ("3.10", "3.11", "3.12", "3.13", "3.14"):
        assert f"Programming Language :: Python :: {version}" in project["classifiers"]
    base = "https://github.com/mamertofabian/maid-validator-rust"
    assert project["urls"]["Repository"] == base
    assert project["urls"]["Issues"] == base + "/issues"
    assert project["urls"]["Changelog"] == base + "/blob/main/CHANGELOG.md"


def test_ci_uses_locked_quality_gates_and_published_dependencies():
    ci = _workflow("ci.yml")
    assert ci["on"] == {"push": {"branches": ["main"]}, "pull_request": ""}
    job = ci["jobs"]["test"]
    assert job["strategy"]["matrix"]["python-version"] == [
        "3.10",
        "3.11",
        "3.12",
        "3.13",
        "3.14",
    ]
    scripts = "\n".join(s.get("run", "") for s in job["steps"])
    for command in (
        "cargo --version",
        "uv sync --locked",
        "uv run pytest -q",
        "uv run ruff check src/ tests/",
        "uv run black --check src/ tests/",
        "uv run maid validate",
        "uv run maid test",
    ):
        assert command in scripts
    assert all(
        s.get("with", {}).get("repository") != "mamertofabian/maid-runner"
        for s in job["steps"]
    )
    assert "defaults" not in job
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "sources" not in config.get("tool", {}).get("uv", {})
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    runner = next(p for p in lock["package"] if p["name"] == "maid-runner")
    assert runner["version"] == "2.27.6"
    assert runner["source"] == {"registry": "https://pypi.org/simple"}


def test_workflows_pin_actions_and_limit_writing_to_publication_jobs():
    for name in ("ci.yml", "publish.yml"):
        workflow = _workflow(name)
        assert workflow["permissions"] == {"contents": "read"}
        for job_name, job in workflow["jobs"].items():
            permissions = job.get("permissions", {})
            assert permissions.get("id-token") != "write" or job_name == "publish"
            assert (
                permissions.get("contents") != "write" or job_name == "github-release"
            )
            for step in job["steps"]:
                if "uses" in step:
                    assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", step["uses"])
                if step.get("uses", "").startswith("actions/checkout@"):
                    assert step["with"]["persist-credentials"] == "false"


def test_publish_verifies_distributions_before_oidc_and_attaches_same_assets():
    workflow = _workflow("publish.yml")
    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    jobs = workflow["jobs"]
    assert jobs["test"]["strategy"]["matrix"]["python-version"] == [
        "3.10",
        "3.11",
        "3.12",
        "3.13",
        "3.14",
    ]
    for name, parent in [
        ("build", "test"),
        ("publish", "build"),
        ("github-release", "publish"),
    ]:
        assert jobs[name]["needs"] == parent
    assert jobs["publish"]["environment"] == {
        "name": "pypi",
        "url": "https://pypi.org/p/maid-validator-rust",
    }
    assert jobs["publish"]["permissions"] == {"id-token": "write"}
    assert jobs["github-release"]["permissions"] == {"contents": "write"}
    scripts = "\n".join(s.get("run", "") for s in jobs["build"]["steps"])
    for evidence in (
        "merge-base --is-ancestor",
        "origin/main",
        "uv build",
        "twine==7.0.0",
        "twine check",
        "uv pip install",
        "maid validators --json",
        "RustValidator",
        '".rs"',
        '"active"',
        "maid-validator-rust ",
    ):
        assert evidence in scripts
    assert "SolidityValidator" not in scripts and "CSharpValidator" not in scripts
    assert any(
        s.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
        for s in jobs["publish"]["steps"]
    )
    upload = next(
        s
        for s in jobs["build"]["steps"]
        if s.get("uses", "").startswith("actions/upload-artifact@")
    )
    assert upload["with"] == {
        "name": "python-package-distributions",
        "path": "dist/",
        "if-no-files-found": "error",
    }
    for name in ("publish", "github-release"):
        download = next(
            s
            for s in jobs[name]["steps"]
            if s.get("uses", "").startswith("actions/download-artifact@")
        )
        assert download["with"] == {
            "name": "python-package-distributions",
            "path": "dist/",
        }
    release = jobs["github-release"]["steps"][-1]
    assert "gh release create" in release["run"] and "dist/*" in release["run"]
    assert "TWINE_PASSWORD" not in str(workflow) and "PYPI_API_TOKEN" not in str(
        workflow
    )


@pytest.mark.parametrize("development_source", [True, False])
def test_publish_rejects_development_source_before_installing(
    tmp_path, development_source
):
    job = _workflow("publish.yml")["jobs"]["test"]
    steps = job["steps"]
    guard = next(
        s for s in steps if s.get("name") == "Reject development-only Runner source"
    )
    install = next(s for s in steps if s.get("name") == "Install locked dependencies")
    assert steps.index(guard) < steps.index(install)
    content = '[project]\nname="fixture"\n'
    if development_source:
        content += (
            '[tool.uv.sources]\nmaid-runner={path="../maid-runner",editable=true}\n'
        )
    (tmp_path / "pyproject.toml").write_text(content)
    result = subprocess.run(
        ["bash", "-eu", "-c", guard["run"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode != 0) is development_source
    if development_source:
        assert "Remove [tool.uv.sources]" in result.stdout


@pytest.mark.parametrize("tag,accepted", [("v0.1.0", True), ("v9.9.9", False)])
def test_publish_version_guard_executes_and_rejects_mismatches(tag, accepted):
    step = _step("Verify tag matches project.version")
    assert step["env"] == {"RELEASE_TAG": "${{ github.ref_name }}"}
    script = step["run"].split("python - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env={**os.environ, "RELEASE_TAG": tag},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted
    if not accepted:
        assert "does not match project.version" in result.stderr


@pytest.mark.parametrize("on_main", [True, False])
def test_publish_main_guard_executes_and_rejects_unmerged_commits(tmp_path, on_main):
    environment = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.test",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.test",
    }

    def git(*args, input=None):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            env=environment,
            input=input,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    git("init", "--initial-branch=main")
    tree = git("mktree", input="")
    base = git("commit-tree", tree, input="base\n")
    main = git("commit-tree", tree, "-p", base, input="main\n")
    other = git("commit-tree", tree, "-p", base, input="other\n")
    git("update-ref", "refs/heads/main", main)
    git("clone", "--bare", ".", str(tmp_path / "origin.git"))
    git("remote", "add", "origin", str(tmp_path / "origin.git"))
    step = _step("Verify tagged commit is on main")
    result = subprocess.run(
        ["bash", "-eu", "-c", step["run"]],
        cwd=tmp_path,
        env={**environment, "GITHUB_SHA": main if on_main else other},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is on_main


def test_release_documentation_names_publisher_setup_and_runner_prerequisite():
    releasing = (ROOT / "RELEASING.md").read_text()
    for label, value in [
        ("PyPI project name", "maid-validator-rust"),
        ("GitHub owner", "mamertofabian"),
        ("GitHub repository", "maid-validator-rust"),
        ("Workflow filename", "publish.yml"),
        ("Environment name", "pypi"),
    ]:
        assert f"- {label}: `{value}`" in releasing
    for detail in (
        "PyPI Trusted Publisher",
        "2.27.7",
        "[tool.uv.sources]",
        "uv lock",
        "uv sync --locked",
        "twine check",
        "unpublished",
        "2.27.6",
    ):
        assert detail in releasing
    readme = (ROOT / "README.md").read_text()
    assert "RELEASING.md" in readme and "CHANGELOG.md" in readme
    assert "0.1.0" in readme and "published on PyPI and GitHub" in readme
    assert "collection" in readme and "2.27.7" in readme
    assert "## 0.1.0 — 2026-10-02" in (ROOT / "CHANGELOG.md").read_text()
