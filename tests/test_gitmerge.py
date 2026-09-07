import subprocess

import pytest

from codegraph.gitmerge import install, merge_driver


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          capture_output=True, text=True)


def test_merge_driver_keeps_ours_and_drops_sentinel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "graphify-out"
    out.mkdir()
    ours = out / "graph.json"
    ours.write_text('{"ours": true}')
    rc = merge_driver("base.tmp", "ours.tmp", "theirs.tmp",
                      "graphify-out/graph.json")
    assert rc == 0
    assert ours.read_text() == '{"ours": true}'          # untouched
    assert (out / ".needs-rebuild").exists()


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_install_configures_repo(tmp_path):
    _git(tmp_path, "init")
    notes = install(tmp_path)
    assert any("gitattributes" in n for n in notes)
    attrs = (tmp_path / ".gitattributes").read_text()
    assert "graphify-out/** merge=codegraph" in attrs
    drv = _git(tmp_path, "config", "--get", "merge.codegraph.driver").stdout.strip()
    assert drv == "codegraph merge-driver %O %A %B %P"


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_real_merge_conflict_resolves(tmp_path):
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@t.t")
    _git(tmp_path, "config", "user.name", "t")
    install(tmp_path)
    # the driver must be resolvable by name for git to call it
    import shutil
    import sys

    _git(tmp_path, "config", "merge.codegraph.driver",
         f'"{sys.executable}" -m codegraph merge-driver %O %A %B %P')

    out = tmp_path / "graphify-out"
    out.mkdir()
    (out / "graph.json").write_text('{"v": 0}')
    (tmp_path / "src.py").write_text("x = 0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "base")

    _git(tmp_path, "checkout", "-b", "feature")
    (out / "graph.json").write_text('{"v": 2}')
    _git(tmp_path, "commit", "-am", "feature build")

    _git(tmp_path, "checkout", "master" if _has_branch(tmp_path, "master") else "main")
    (out / "graph.json").write_text('{"v": 1}')
    _git(tmp_path, "commit", "-am", "main build")

    # merge must succeed (driver returns 0), keeping our version
    r = subprocess.run(["git", "-C", str(tmp_path), "merge", "feature"],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert (out / "graph.json").read_text() == '{"v": 1}'
    assert (out / ".needs-rebuild").exists()


def _has_branch(root, name):
    r = subprocess.run(["git", "-C", str(root), "branch", "--list", name],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())
