import subprocess

from above_all.paths import find_project_root, resolve_scopes


def test_finds_git_root(tmp_path):
    subprocess.run(["git","init",str(tmp_path)],check=True,capture_output=True)
    child=tmp_path/"a"; child.mkdir()
    assert find_project_root(child) == tmp_path


def test_outside_git_has_only_global(tmp_path, monkeypatch):
    monkeypatch.setenv("ABOVE_ALL_HOME",str(tmp_path/"home"))
    scopes=resolve_scopes(tmp_path)
    assert scopes.global_root == tmp_path/"home" and scopes.project_root is None
