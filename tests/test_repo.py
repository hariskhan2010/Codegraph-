"""`codegraph extract <git-url>` — clone (cached) then build."""

import subprocess

import pytest

from codegraph.config import db_path
from codegraph.db import Db
from codegraph.pipeline import extract
from codegraph.repo import _slug, is_git_url, resolve_source


@pytest.mark.parametrize("url,expect", [
    ("https://github.com/owner/repo", True),
    ("https://github.com/owner/repo.git", True),
    ("git@github.com:owner/repo.git", True),
    ("git://example.com/repo", True),
    ("/home/me/project", False),
    ("./relative", False),
    ("C:\\Users\\me\\project", False),
])
def test_is_git_url(url, expect):
    assert is_git_url(url) is expect


def test_slug_is_filesystem_safe():
    assert _slug("https://github.com/Owner/My.Repo.git") == "github.com-Owner-My.Repo"
    assert _slug("git@gitlab.com:group/sub/proj.git") == "gitlab.com-group-sub-proj"


def test_resolve_local_path_is_identity(tmp_path):
    assert resolve_source(str(tmp_path)) == tmp_path


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_extract_clones_and_builds_a_local_bare_repo(tmp_path):
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "lib.py").write_text("def helper(x):\n    return x + 1\n")
    (origin / "app.py").write_text(
        "from lib import helper\n\ndef run(x):\n    return helper(x)\n"
    )
    for args in (["init", "-q"], ["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-qm", "init"]):
        subprocess.run(["git", *args], cwd=origin, check=True,
                       capture_output=True)

    dest = tmp_path / "checkout"
    local = resolve_source(origin.as_uri(), dest=dest)
    assert (local / ".git").is_dir()

    extract(local, semantic="none", scip="none")
    db = Db(db_path(local), create=False)
    labels = {r["label"] for r in db.nodes()}
    db.close()
    assert {"helper()", "run()"} <= labels
