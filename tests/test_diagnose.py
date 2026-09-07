from codegraph.analyze import suggested_questions
from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.diagnose import diagnose, format_report
from codegraph.pipeline import extract


def _proj(tmp_path):
    (tmp_path / "a.py").write_text(
        "def handler(req):\n    return service(req)\n\n"
        "def service(x):\n    return store(x)\n\n"
        "def store(x):\n    return x\n"
    )
    extract(tmp_path, semantic="none")
    return Db(db_path(tmp_path), create=False)


def test_diagnose_all_green_on_clean_graph(tmp_path):
    db = _proj(tmp_path)
    d = diagnose(db)
    db.close()
    names = {c["check"] for c in d["checks"]}
    assert {"parse_coverage", "referential_integrity", "provenance"} <= names
    assert "referential_integrity" not in d["failed"]
    assert isinstance(format_report(d), str)


def test_suggested_questions_and_report(tmp_path):
    db = _proj(tmp_path)
    qs = suggested_questions(db)
    assert qs and any("work" in q.lower() for q in qs)
    from codegraph.analyze import analyze
    from codegraph.render.report import write_report

    analyze(db)
    write_report(db, out_dir(tmp_path))
    md = (out_dir(tmp_path) / "GRAPH_REPORT.md").read_text()
    db.close()
    assert "Suggested Questions" in md
