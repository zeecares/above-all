import subprocess

import pytest

from above_all.cli import init_scopes
from above_all.privacy import ensure_project_privacy


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def make_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("ABOVE_ALL_HOME", str(tmp_path / "home"))
    return repo


def test_init_defaults_every_project_artifact_to_local_only(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, monkeypatch)
    scopes = init_scopes()

    assert (scopes.project_root / ".gitignore").read_text() == "*\n"
    for relative in (
        "assistant.db",
        "candidates/draft.md",
        "sessions/run/transcript.jsonl",
        "notes/approved.md",
        "AGENT_CONTEXT.md",
    ):
        path = scopes.project_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
        assert git(repo, "check-ignore", path.relative_to(repo).as_posix())


def test_explicit_shared_policy_only_allows_approved_knowledge(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, monkeypatch)
    scopes = init_scopes(share_approved=True)

    private = scopes.project_root / "sessions" / "run" / "transcript.jsonl"
    approved = scopes.project_root / "notes" / "team.md"
    private.parent.mkdir(parents=True)
    approved.parent.mkdir(parents=True, exist_ok=True)
    private.touch()
    approved.touch()

    assert git(repo, "check-ignore", private.relative_to(repo).as_posix())
    shown = git(repo, "status", "--short", "--untracked-files=all")
    assert ".above-all/notes/team.md" in shown
    assert "transcript.jsonl" not in shown


def test_init_refuses_private_state_already_tracked(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, monkeypatch)
    tracked = repo / ".above-all" / "assistant.db"
    tracked.parent.mkdir()
    tracked.touch()
    git(repo, "add", "-f", tracked.relative_to(repo).as_posix())

    with pytest.raises(RuntimeError, match="unsafe tracked"):
        ensure_project_privacy(repo)
