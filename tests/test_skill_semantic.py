"""Skill-driven semantic pass — `extract --semantic skill` writes a request,
`apply-semantic` ingests the response. No API key, graphify-style."""

import json

from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.pipeline import extract
from codegraph.semantic import apply_response, write_request


def _proj(tmp_path):
    (tmp_path / "auth.py").write_text(
        "def login(u):\n    return _verify(u)\n\ndef _verify(u):\n    return u is not None\n"
    )
    (tmp_path / "cart.py").write_text(
        "def add_item(c, i):\n    return _total(c)\n\ndef _total(c):\n    return sum(c)\n"
    )
    return tmp_path


def test_extract_skill_writes_request_and_no_model_call(tmp_path):
    proj = _proj(tmp_path)
    st = extract(proj, semantic="skill", scip="none")
    req_meta = st["semantic_request"]
    assert req_meta and req_meta["files"] == 2
    req = json.loads((out_dir(proj) / "semantic-request.json").read_text())
    assert {f["file"] for f in req["files"]} == {"auth.py", "cart.py"}
    assert req["files"][0]["symbols"]
    assert "source" in req["files"][0]
    assert req["communities"]
    # semantic pass did NOT run — no rationale yet
    db = Db(db_path(proj), create=False)
    assert not db.conn.execute(
        "SELECT 1 FROM nodes WHERE rationale IS NOT NULL LIMIT 1").fetchone()
    db.close()


def test_apply_semantic_ingests_annotations_and_names(tmp_path):
    proj = _proj(tmp_path)
    extract(proj, semantic="skill", scip="none")
    db = Db(db_path(proj), create=False)
    ids = [r["id"] for r in db.conn.execute("SELECT id FROM communities ORDER BY id")]

    response = {
        "annotations": {
            "auth.py": [
                {"label": "login()", "line": 1, "rationale": "authenticate a user",
                 "concepts": ["auth"]},
                {"label": "_verify()", "line": 4, "rationale": "check id present"},
            ],
        },
        "ambiguous_edges": {},
        "community_names": {str(ids[0]): "User Authentication",
                            str(ids[-1]): "Shopping Cart"},
    }
    r = apply_response(db, proj, response)
    assert r["annotated"] == 2
    assert r["communities_named"] >= 1

    row = db.conn.execute(
        "SELECT rationale, community_name FROM nodes WHERE label='login()'"
    ).fetchone()
    db.close()
    assert row["rationale"] == "authenticate a user"
    assert row["community_name"] in ("User Authentication", "Shopping Cart")


def test_apply_semantic_reads_response_file(tmp_path):
    proj = _proj(tmp_path)
    extract(proj, semantic="skill", scip="none")
    (out_dir(proj) / "semantic-response.json").write_text(json.dumps({
        "annotations": {"cart.py": [
            {"label": "add_item()", "line": 1, "rationale": "add an item to the cart"}]},
        "community_names": {},
    }))
    db = Db(db_path(proj), create=False)
    r = apply_response(db, proj, out_dir(proj) / "semantic-response.json")
    got = db.conn.execute(
        "SELECT rationale FROM nodes WHERE label='add_item()'").fetchone()["rationale"]
    db.close()
    assert r["annotated"] == 1 and got == "add an item to the cart"
