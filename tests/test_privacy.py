import subprocess
from pathlib import Path

import pytest

from above_all.cli import init_scopes, main
from above_all.notes import create_candidate, set_note_status
from above_all.privacy import ensure_project_privacy, validate_project_privacy


def git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=check, capture_output=True, text=True
    )


def make_repo(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("ABOVE_ALL_HOME", str(tmp_path / "home"))
    return repo


def touch(repo: Path, relative: str) -> Path:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    return path


def active_note(repo: Path, note_id="a" * 32) -> Path:
    candidate = create_candidate(
        repo / ".above-all" / "candidates", "Approved", "reviewed", "fact", ["test:1"]
    )
    candidate = candidate.rename(candidate.with_name(f"{note_id}.md"))
    active = repo / ".above-all" / "notes" / candidate.name
    active.parent.mkdir(parents=True, exist_ok=True)
    candidate.rename(active)
    return set_note_status(active, "active")


PRIVATE_PATHS = (
    "assistant.db",
    "assistant.db-wal",
    "assistant.db-shm",
    "candidates/draft.md",
    "sessions/run/transcript.jsonl",
    "traces/input.jsonl",
    "logs/runner.log",
    "analysis/system-candidates.json",
    "sessions/run/SELECTED_SKILLS.md",
    "skills/local/SKILL.md",
    "future-secret/token",
)


def test_default_policy_is_status_visible_and_every_artifact_is_local(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, monkeypatch)
    scopes = init_scopes()

    policy = scopes.project_root / ".gitignore"
    assert policy.read_text() == "*\n!.gitignore\n"
    shown = git(repo, "status", "--short", "--untracked-files=all").stdout
    assert "?? .above-all/.gitignore" in shown

    for relative in (*PRIVATE_PATHS, "notes/approved.md", "AGENT_CONTEXT.md"):
        path = touch(repo, f".above-all/{relative}")
        assert git(repo, "check-ignore", path.relative_to(repo).as_posix()).returncode == 0


@pytest.mark.parametrize("relative", (*PRIVATE_PATHS, "notes/approved.md", "AGENT_CONTEXT.md"))
def test_default_rejects_every_pretracked_path(tmp_path, monkeypatch, relative):
    repo = make_repo(tmp_path, monkeypatch)
    path = touch(repo, f".above-all/{relative}")
    git(repo, "add", "-f", path.relative_to(repo).as_posix())
    with pytest.raises(RuntimeError, match="unsafe tracked"):
        ensure_project_privacy(repo)


def test_share_policy_exposes_only_valid_active_notes_and_context(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, monkeypatch)
    approved = active_note(repo)
    invalid_name = touch(repo, ".above-all/notes/not-approved-secret.txt")
    inactive = touch(repo, f".above-all/notes/{'b' * 32}.md")
    context = touch(repo, ".above-all/AGENT_CONTEXT.md")

    scopes = init_scopes(share_approved=True)
    policy = scopes.project_root / ".gitignore"
    shown = git(repo, "status", "--short", "--untracked-files=all").stdout
    for exposed in (policy, approved, context):
        assert f"?? {exposed.relative_to(repo).as_posix()}" in shown
    for hidden in (invalid_name, inactive):
        assert hidden.relative_to(repo).as_posix() not in shown
    for relative in PRIVATE_PATHS:
        hidden = touch(repo, f".above-all/{relative}")
        assert git(repo, "check-ignore", hidden.relative_to(repo).as_posix()).returncode == 0


@pytest.mark.parametrize("relative", PRIVATE_PATHS)
def test_share_mode_rejects_known_and_future_private_pretracked_paths(
    tmp_path, monkeypatch, relative
):
    repo = make_repo(tmp_path, monkeypatch)
    path = touch(repo, f".above-all/{relative}")
    git(repo, "add", "-f", path.relative_to(repo).as_posix())
    with pytest.raises(RuntimeError, match="unsafe tracked"):
        ensure_project_privacy(repo, share_approved=True)


def test_share_mode_accepts_only_exact_valid_public_files(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, monkeypatch)
    approved = active_note(repo)
    ensure_project_privacy(repo, share_approved=True)
    for path in (repo / ".above-all/.gitignore", approved, repo / ".above-all/AGENT_CONTEXT.md"):
        touch(repo, path.relative_to(repo).as_posix())
        git(repo, "add", path.relative_to(repo).as_posix())
    assert validate_project_privacy(repo, share_approved=True)["status"] == "safe"

    invalid = touch(repo, f".above-all/notes/{'c' * 32}.md")
    git(repo, "add", "-f", invalid.relative_to(repo).as_posix())
    with pytest.raises(RuntimeError, match="unsafe tracked"):
        validate_project_privacy(repo, share_approved=True)


def test_privacy_check_command_validates_installed_policy(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path, monkeypatch)
    init_scopes(share_approved=True)
    assert main(["privacy-check", "--share-approved"]) == 0
    assert '"status": "safe"' in capsys.readouterr().out
    (repo / ".above-all/.gitignore").write_text("*\n")
    with pytest.raises(RuntimeError, match="does not match"):
        main(["privacy-check", "--share-approved"])
