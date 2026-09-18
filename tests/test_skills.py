import pytest

from above_all.skills import discover, load_selected


def write_skill(root, name, description="Use when testing.", body="Do the thing."):
    path = root / "skills" / name / "SKILL.md"; path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n")
    return path


def test_project_skill_shadows_global(tmp_path):
    global_root = tmp_path / "global"; project_root = tmp_path / "project"
    write_skill(global_root, "pr", body="global"); project = write_skill(project_root, "pr", body="project")
    assert discover(global_root, project_root)["pr"].path == project


def test_selected_skill_is_explicit_and_ordered(tmp_path):
    write_skill(tmp_path, "pr"); write_skill(tmp_path, "tdd")
    assert [s.name for s in load_selected(tmp_path, None, ["tdd", "pr"])] == ["tdd", "pr"]


def test_missing_trigger_fails_loudly(tmp_path):
    write_skill(tmp_path, "bad", description="")
    with pytest.raises(ValueError, match="description"):
        discover(tmp_path, None)


def test_unknown_selected_skill_fails_loudly(tmp_path):
    with pytest.raises(ValueError, match="unknown skill"):
        load_selected(tmp_path, None, ["missing"])

