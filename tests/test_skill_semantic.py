"""Skill-driven semantic pass — `extract --semantic skill` writes a request,
`apply-semantic` ingests the response. No API key, graphify-style."""

import json

from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.pipeline import extract
from codegraph.semantic import CHUNK_DIR, apply_response, write_request


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


def test_skill_request_fans_out_into_chunks(tmp_path):
    for i in range(7):
        (tmp_path / f"mod{i}.py").write_text(
            f"def f{i}(x):\n    return g{i}(x)\n\ndef g{i}(x):\n    return x\n"
        )
    extract(tmp_path, semantic="skill", scip="none", semantic_chunk_files=3)
    out = out_dir(tmp_path)
    index = json.loads((out / "semantic-request.json").read_text())
    assert index["chunked"] is True and index["chunks"] == 3
    cdir = out / CHUNK_DIR
    reqs = sorted(cdir.glob("request-*.json"))
    assert [p.name for p in reqs] == [
        "request-001.json", "request-002.json", "request-003.json"]
    assert (cdir / "communities.json").exists()
    seen = set()
    for p in reqs:
        d = json.loads(p.read_text())
        assert 0 < len(d["files"]) <= 3
        seen |= {f["file"] for f in d["files"]}
    assert seen == {f"mod{i}.py" for i in range(7)}


def test_apply_semantic_merges_chunk_responses(tmp_path):
    for i in range(7):
        (tmp_path / f"mod{i}.py").write_text(f"def f{i}(x):\n    return x\n")
    extract(tmp_path, semantic="skill", scip="none", semantic_chunk_files=3)
    cdir = out_dir(tmp_path) / CHUNK_DIR
    db = Db(db_path(tmp_path), create=False)
    cids = [r["id"] for r in db.conn.execute("SELECT id FROM communities ORDER BY id")]

    # one subagent per chunk writes its own response file
    for k, p in enumerate(sorted(cdir.glob("request-*.json")), 1):
        files = [f["file"] for f in json.loads(p.read_text())["files"]]
        (cdir / f"response-{k:03d}.json").write_text(json.dumps({
            "annotations": {
                fn: [{"label": f"f{fn[3]}()", "line": 1,
                      "rationale": f"entry point of {fn}"}]
                for fn in files
            }
        }))
    (cdir / "communities-response.json").write_text(json.dumps(
        {"community_names": {str(cids[0]): "Module Fixtures"}}))

    r = apply_response(db, tmp_path, None)
    assert r["merged_chunks"] == 3
    assert r["annotated"] == 7 and r["communities_named"] >= 1
    got = db.conn.execute(
        "SELECT rationale FROM nodes WHERE label='f3()'").fetchone()["rationale"]
    db.close()
    assert got == "entry point of mod3.py"


def test_ideas_request_written_and_concept_graph_ingested(tmp_path):
    for i in range(7):
        (tmp_path / f"mod{i}.py").write_text(f"def f{i}(x):\n    return x\n")
    (tmp_path / "ARCHITECTURE.md").write_text(
        "# Architecture\n## Tenant Isolation\nRLS plus app-layer checks.\n"
    )
    (tmp_path / "SECURITY.md").write_text(
        "# Security\n## Two-Layer Defence\nDefence in depth for tenant data.\n"
    )
    extract(tmp_path, semantic="skill", scip="none", semantic_chunk_files=3)
    cdir = out_dir(tmp_path) / CHUNK_DIR
    index = json.loads((out_dir(tmp_path) / "semantic-request.json").read_text())
    assert index["ideas"] is True and index["docs"] == 2
    ideas_req = json.loads((cdir / "ideas.json").read_text())
    assert {d["file"] for d in ideas_req["docs"]} == {"ARCHITECTURE.md", "SECURITY.md"}

    # a subagent writes the idea graph; concepts link across the two docs
    (cdir / "ideas-response.json").write_text(json.dumps({
        "concepts": [
            {"label": "Tenant Isolation", "kind": "concept",
             "anchor_file": "ARCHITECTURE.md", "rationale": "keep tenants apart",
             "tags": ["security"]},
            {"label": "Two-Layer Defence", "kind": "concept",
             "anchor_file": "SECURITY.md", "rationale": "defence in depth"},
        ],
        "idea_edges": [
            {"src": "Two-Layer Defence", "dst": "Tenant Isolation",
             "relation": "semantically_similar_to", "confidence": "INFERRED",
             "confidence_score": 0.85, "why": "same goal, different doc"},
        ],
    }))
    for k in (1, 2, 3):
        (cdir / f"response-{k:03d}.json").write_text('{"annotations": {}}')

    db = Db(db_path(tmp_path), create=False)
    r = apply_response(db, tmp_path, None)
    assert r["concepts"] == 2 and r["idea_edges"] == 1
    row = db.conn.execute(
        "SELECT id FROM nodes WHERE label='Tenant Isolation' AND kind='concept'"
    ).fetchone()
    assert row is not None
    e = db.conn.execute(
        "SELECT relation, evidence FROM edges WHERE evidence='llm-idea'"
    ).fetchone()
    db.close()
    assert e["relation"] == "semantically_similar_to"


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
