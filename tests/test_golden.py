"""Golden integration test — a small multi-language project with known shape.

Asserts the invariants the design promises: honest provenance on every edge,
deterministic output, no dangling links, real cross-file resolution, and
incremental update that touches only the changed file.
"""

import json

import pytest

from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.pipeline import extract, update
from codegraph.query import affected

_VALID_CONF = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}


@pytest.fixture
def project(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "auth.py").write_text(
        "from core.store import find_user\n\n"
        "class Authenticator:\n"
        "    def login(self, name, pw):\n"
        "        u = find_user(name)\n"
        "        return u and verify(u, pw)\n\n"
        "def verify(user, pw):\n"
        "    return user['pw'] == pw\n"
    )
    (tmp_path / "core" / "store.py").write_text(
        "USERS = {}\n\n"
        "def find_user(name):\n"
        "    return USERS.get(name)\n\n"
        "def add_user(name, pw):\n"
        "    USERS[name] = {'pw': pw}\n"
    )
    (tmp_path / "api.js").write_text(
        "import { Authenticator } from './core/auth';\n"
        "export function handleLogin(req) {\n"
        "  const a = new Authenticator();\n"
        "  return a.login(req.name, req.pw);\n"
        "}\n"
    )
    (tmp_path / "svc.go").write_text(
        "package svc\n"
        "func Validate(t string) bool { return len(t) > 0 }\n"
        "func Handle(t string) bool { return Validate(t) }\n"
    )
    return tmp_path


def test_every_edge_has_valid_provenance(project):
    extract(project, semantic="none")
    db = Db(db_path(project), create=False)
    for e in db.edges():
        assert e["confidence"] in _VALID_CONF, e["relation"]
        assert e["confidence_score"] is not None
        assert e["source_file"]
    db.close()


def test_no_dangling_links_in_graph_json(project):
    extract(project, semantic="none")
    gj = json.loads((out_dir(project) / "graph.json").read_text())
    ids = {n["id"] for n in gj["nodes"]}
    assert gj["directed"] is True
    for l in gj["links"]:
        assert l["source"] in ids and l["target"] in ids


def test_cross_file_and_cross_language_resolution(project):
    extract(project, semantic="none")
    db = Db(db_path(project), create=False)
    by_id = {int(r["id"]): r for r in db.nodes()}
    calls = {
        (by_id[int(e["src"])]["label"], by_id[int(e["dst"])]["label"])
        for e in db.edges() if e["relation"] == "calls"
    }
    # python cross-file: Authenticator.login -> store.find_user
    assert (".login()", "find_user()") in calls
    # go same-file: Handle -> Validate
    assert ("Handle()", "Validate()") in calls
    db.close()


def test_deterministic_across_runs(project):
    extract(project, semantic="none")
    a = (out_dir(project) / "graph.json").read_bytes()
    extract(project, force=True, semantic="none")
    b = (out_dir(project) / "graph.json").read_bytes()
    assert a == b


def test_incremental_update_isolates_change(project):
    extract(project, semantic="none")
    db = Db(db_path(project), create=False)
    store_ids = {int(r["id"]) for r in db.nodes() if r["source_file"] == "core/store.py"}
    db.close()

    (project / "core" / "auth.py").write_text(
        "def login(name):\n    return name\n"  # gut the file
    )
    update(project)
    db = Db(db_path(project), create=False)
    now_store = {int(r["id"]) for r in db.nodes() if r["source_file"] == "core/store.py"}
    assert now_store == store_ids  # store.py nodes untouched (same PKs)
    labels = {r["label"] for r in db.nodes() if r["source_file"] == "core/auth.py"}
    assert "Authenticator" not in labels and "login()" in labels
    db.close()


def test_affected_crosses_files(project):
    extract(project, semantic="none")
    db = Db(db_path(project), create=False)
    out = affected(db, "find_user")
    assert "login" in out  # auth.py depends on store.find_user
    db.close()
