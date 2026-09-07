"""SCIP tier — scope-aware edges the single-definition guard can't produce."""

import json

from codegraph.config import db_path
from codegraph.db import Db
from codegraph.pipeline import extract
from codegraph.scip import ingest, run


def _proj(tmp_path):
    (tmp_path / "store.py").write_text("def save(x):\n    return x\n")
    (tmp_path / "cache.py").write_text("def save(x):\n    return x\n")
    (tmp_path / "app.py").write_text(
        "def run():\n"
        "    save(1)\n"
        "    return 2\n"
    )
    extract(tmp_path, semantic="none", scip="none")
    return tmp_path


_INDEX = {
    "documents": [
        {"relative_path": "store.py", "occurrences": [
            {"range": [0, 4, 8], "symbol": "scip-python . . store/save().",
             "symbol_roles": 1}]},
        {"relative_path": "cache.py", "occurrences": [
            {"range": [0, 4, 8], "symbol": "scip-python . . cache/save().",
             "symbol_roles": 1}]},
        {"relative_path": "app.py", "occurrences": [
            {"range": [0, 4, 7], "symbol": "scip-python . . app/run().",
             "symbol_roles": 1},
            {"range": [1, 4, 8], "symbol": "scip-python . . store/save().",
             "symbol_roles": 8}]},
    ]
}


def test_scip_disambiguates_overloaded_name(tmp_path):
    proj = _proj(tmp_path)
    db = Db(db_path(proj), create=False)

    # the heuristic tier leaves the ambiguous call unresolved
    by_id = {int(r["id"]): r for r in db.nodes()}
    pre = {(by_id[int(e["src"])]["label"], by_id[int(e["dst"])]["source_file"])
           for e in db.edges() if e["relation"] == "calls"}
    assert ("run()", "store.py") not in pre

    stats = ingest(db, _INDEX, proj)
    assert stats["edges"] >= 1

    scip_edges = [e for e in db.edges() if e["evidence"] == "scip"]
    by_id = {int(r["id"]): r for r in db.nodes()}
    pairs = {(by_id[int(e["src"])]["label"], by_id[int(e["dst"])]["source_file"],
              e["confidence"]) for e in scip_edges}
    db.close()
    assert ("run()", "store.py", "EXTRACTED") in pairs
    assert not any(f == "cache.py" for _, f, _ in pairs)


def test_scip_run_reads_json_index(tmp_path):
    proj = _proj(tmp_path)
    (proj / "index.json").write_text(json.dumps(_INDEX))
    db = Db(db_path(proj), create=False)
    stats = run(db, proj, index_path=proj / "index.json")
    db.close()
    assert stats and stats["edges"] >= 1
    assert stats["index"].endswith("index.json")


def test_scip_ingest_is_idempotent(tmp_path):
    proj = _proj(tmp_path)
    db = Db(db_path(proj), create=False)
    ingest(db, _INDEX, proj)
    first = len([e for e in db.edges() if e["evidence"] == "scip"])
    ingest(db, _INDEX, proj)
    second = len([e for e in db.edges() if e["evidence"] == "scip"])
    db.close()
    assert first == second and first >= 1


def test_extract_auto_detects_scip_index(tmp_path):
    (tmp_path / "store.py").write_text("def save(x):\n    return x\n")
    (tmp_path / "cache.py").write_text("def save(x):\n    return x\n")
    (tmp_path / "app.py").write_text("def run():\n    save(1)\n    return 2\n")
    (tmp_path / "index.scip").write_bytes(b"")  # presence only; no scip CLI in CI
    st = extract(tmp_path, semantic="none")
    # with no `scip` binary the tier is a graceful no-op, not an error
    assert st["nodes"] > 0
