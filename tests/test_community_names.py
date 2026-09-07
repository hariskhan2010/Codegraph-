"""Community labelling — heuristic (dir + top symbols) and optional LLM pass."""

import json

from codegraph import llm
from codegraph.cluster import _name_community
from codegraph.config import db_path
from codegraph.db import Db
from codegraph.pipeline import extract


class _Row(dict):
    def __getitem__(self, k):
        return self.get(k)


def _m(label, kind="function", degree=1, source_file="src/signals/score.py",
       file_type="code", source_location="L1"):
    return _Row(label=label, kind=kind, degree=degree, source_file=source_file,
               file_type=file_type, source_location=source_location)


def test_heuristic_name_uses_dir_and_top_symbols():
    members = [
        _m("score()", degree=9, source_file="backend/app/signals/score.py"),
        _m("ScoringEngine", "class", 6, "backend/app/signals/engine.py"),
        _m("main()", degree=12, source_file="backend/app/signals/run.py"),  # generic
        _m("signals.py", "file", 0, "backend/app/signals/signals.py"),
    ]
    name = _name_community(0, members)
    assert name.startswith("Signals:")
    assert "score" in name and "main" not in name  # generic name skipped


def test_doc_community_uses_top_heading():
    members = [
        _m("Deployment Guide", "section", 3, "docs/deploy.md",
           file_type="document", source_location="L1-L40"),
        _m("Prerequisites", "section", 1, "docs/deploy.md",
           file_type="document", source_location="L5-L12"),
    ]
    assert _name_community(0, members) == "Deployment Guide"


def test_llm_naming_pass_overrides_labels(tmp_path):
    (tmp_path / "auth.py").write_text(
        "def login(u):\n    return verify(u)\n\ndef verify(u):\n    return True\n"
    )
    (tmp_path / "cart.py").write_text(
        "def add_item(c, i):\n    return total(c)\n\ndef total(c):\n    return 0\n"
    )
    extract(tmp_path, semantic="none", scip="none")

    db = Db(db_path(tmp_path), create=False)
    ids = [r["id"] for r in db.conn.execute("SELECT id FROM communities ORDER BY id")]

    def fake(system, user):
        return json.dumps({"names": {str(i): f"Named Cluster {i}" for i in ids}})

    llm.set_mock(fake)
    from codegraph.semantic import name_communities

    r = name_communities(db, llm.make_backend("mock"))
    llm.set_mock(None)
    labels = {row["label"] for row in db.conn.execute("SELECT label FROM communities")}
    db.close()
    assert r["named"] >= 1
    assert any(l.startswith("Named Cluster") for l in labels)
