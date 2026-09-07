import json

from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.pipeline import extract, update
from codegraph.query import affected, explain, query


def _mkproj(root):
    (root / "auth.py").write_text(
        "def login(user):\n"
        "    return check_password(user)\n\n"
        "def check_password(user):\n"
        "    return True\n"
    )
    (root / "api.py").write_text(
        "from auth import login\n\n"
        "def handle_request(req):\n"
        "    return login(req)\n"
    )


def test_extract_end_to_end(tmp_path):
    _mkproj(tmp_path)
    st = extract(tmp_path)
    assert st["nodes"] > 0
    assert st["edges"] > 0

    gj = json.loads((out_dir(tmp_path) / "graph.json").read_text())
    assert gj["directed"] is True
    assert "links" in gj
    ids = {n["id"] for n in gj["nodes"]}
    assert all(l["source"] in ids and l["target"] in ids for l in gj["links"])
    # every link has a confidence_score
    assert all("confidence_score" in l for l in gj["links"])
    # cross-file call resolved: api.handle_request -> auth.login
    calls = {(l["source"], l["target"]) for l in gj["links"] if l["relation"] == "calls"}
    assert ("api_handle_request", "auth_login") in calls


def test_incremental_update_is_deterministic(tmp_path):
    _mkproj(tmp_path)
    extract(tmp_path)
    h1 = (out_dir(tmp_path) / "graph.json").read_bytes()
    update(tmp_path)  # nothing changed
    h2 = (out_dir(tmp_path) / "graph.json").read_bytes()
    assert h1 == h2


def test_incremental_update_replaces_one_file(tmp_path):
    _mkproj(tmp_path)
    extract(tmp_path)
    db = Db(db_path(tmp_path), create=False)
    n_before = db.stats()["nodes"]
    db.close()

    (tmp_path / "auth.py").write_text(
        "def login(user):\n    return True\n\n"
        "def logout(user):\n    return True\n"
    )
    update(tmp_path)
    db = Db(db_path(tmp_path), create=False)
    labels = {r["label"] for r in db.nodes()}
    assert "logout()" in labels
    assert "check_password()" not in labels  # removed with the rewrite
    # api.py nodes untouched
    assert any(r["label"] == "handle_request()" for r in db.nodes())
    db.close()


def test_query_finds_wording_gap(tmp_path):
    (tmp_path / "auth.py").write_text(
        "def authenticate(user):\n    return True\n"
    )
    extract(tmp_path)
    db = Db(db_path(tmp_path), create=False)
    out = query(db, "how does authentication work")
    assert "authenticate" in out
    db.close()


def test_affected_reverse_traversal(tmp_path):
    _mkproj(tmp_path)
    extract(tmp_path)
    db = Db(db_path(tmp_path), create=False)
    out = affected(db, "login")
    assert "handle_request" in out
    db.close()
