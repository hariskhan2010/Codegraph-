import json

from codegraph import llm
from codegraph.config import db_path
from codegraph.db import Db
from codegraph.pipeline import extract
from codegraph.query import explain, query


def _mock_response(system, user):
    # user contains the SYMBOLS listing; annotate whatever "login" is present
    ann = []
    for line in user.splitlines():
        if line.startswith("- "):
            label = line[2:].split("  (")[0]
            ann.append({"label": label, "line": 1,
                        "rationale": f"does {label.strip('.()')}",
                        "concepts": ["auth"]})
    return json.dumps({"annotations": ann, "ambiguous_edges": [
        {"src": ann[0]["label"], "dst": ann[-1]["label"], "relation": "uses",
         "why": "dynamic dispatch"}
    ] if len(ann) >= 2 else []})


def test_semantic_pass_with_mock(tmp_path, monkeypatch):
    (tmp_path / "auth.py").write_text(
        "def login(u):\n    return check(u)\n\ndef check(u):\n    return True\n"
    )
    llm.set_mock(_mock_response)
    try:
        st = extract(tmp_path, semantic="mock")
    finally:
        llm.set_mock(None)

    assert st["semantic"]["files"] == 1
    assert st["semantic"]["annotated"] >= 2

    db = Db(db_path(tmp_path), create=False)
    rows = {r["label"]: r for r in db.nodes()}
    assert rows["login()"]["rationale"] == "does login"
    assert json.loads(rows["login()"]["extra"])["concepts"] == ["auth"]
    # ambiguous edge added
    amb = db.conn.execute(
        "SELECT COUNT(*) n FROM edges WHERE confidence='AMBIGUOUS'"
    ).fetchone()["n"]
    assert amb >= 1
    # rationale is searchable
    assert "login" in query(db, "authentication")
    db.close()


def test_semantic_never_trusts_llm_node_ids(tmp_path):
    (tmp_path / "m.py").write_text("def real():\n    return 1\n")

    def bad(system, user):
        return json.dumps({"annotations": [
            {"label": "totally_made_up", "line": 99, "rationale": "ghost"},
            {"label": "real()", "line": 1, "rationale": "the real one"},
        ], "ambiguous_edges": []})

    llm.set_mock(bad)
    try:
        st = extract(tmp_path, semantic="mock")
    finally:
        llm.set_mock(None)

    assert st["semantic"]["dropped"] == 1  # made-up label dropped
    db = Db(db_path(tmp_path), create=False)
    labels = {r["label"] for r in db.nodes()}
    assert "totally_made_up" not in labels
    assert any(r["rationale"] == "the real one" for r in db.nodes())
    db.close()
