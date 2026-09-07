"""Phase 3 — document tier, clone, PR impact."""

import json

import pytest

from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.extract.docs import extract_doc
from codegraph.pipeline import extract
from codegraph.query import query


def test_markdown_sections_become_nodes():
    text = "# Title\nintro\n## Design\nblah\n### Storage\ndetail\n## Usage\nrun it\n"
    res = extract_doc("docs/spec.md", text, ".md")
    labels = [n["label"] for n in res.nodes]
    assert labels == ["spec.md", "Title", "Design", "Storage", "Usage"]
    # nesting: Storage is under Design, Design+Usage under Title
    slug = {n["label"]: n["slug"] for n in res.nodes}
    pairs = {(e["src_slug"], e["dst_slug"]) for e in res.edges if e["relation"] == "contains"}
    assert (slug["Design"], slug["Storage"]) in pairs
    assert (slug["Title"], slug["Design"]) in pairs


def test_docs_are_queryable_end_to_end(tmp_path):
    (tmp_path / "code.py").write_text("def run():\n    return 1\n")
    (tmp_path / "ARCHITECTURE.md").write_text(
        "# Architecture\n## Retrieval pipeline\n"
        "The retrieval pipeline tokenizes then expands terms.\n"
    )
    extract(tmp_path, semantic="none")
    db = Db(db_path(tmp_path), create=False)
    kinds = {r["kind"] for r in db.nodes()}
    assert "section" in kinds
    out = query(db, "retrieval pipeline")
    db.close()
    assert "Retrieval pipeline" in out


def test_no_docs_flag_skips_them(tmp_path):
    (tmp_path / "code.py").write_text("def run():\n    return 1\n")
    (tmp_path / "NOTES.md").write_text("# Notes\n## Section A\ntext\n")
    extract(tmp_path, semantic="none", docs=False)
    db = Db(db_path(tmp_path), create=False)
    assert "section" not in {r["kind"] for r in db.nodes()}
    db.close()


def test_clone_rewrites_root(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text("def f():\n    return g()\n\ndef g():\n    return 1\n")
    extract(src, semantic="none")

    dest = tmp_path / "dest"
    import shutil

    (dest / out_dir(dest).name).mkdir(parents=True)
    shutil.copyfile(db_path(src), db_path(dest))
    db = Db(db_path(dest), create=False)
    db.set_meta("root", dest.resolve().as_posix())
    db.conn.commit()
    assert db.stats()["nodes"] > 0
    assert db.get_meta("root").endswith("dest")
    db.close()


def test_pr_impact_traverses_graph(tmp_path, monkeypatch):
    (tmp_path / "core.py").write_text("def base():\n    return 1\n")
    (tmp_path / "api.py").write_text(
        "from core import base\n\ndef handler():\n    return base()\n"
    )
    extract(tmp_path, semantic="none")

    from codegraph import prs

    def fake_gh(*args):
        if args[:2] == ("pr", "view"):
            return {"number": 7, "title": "tweak base", "files": [{"path": "core.py"}],
                    "headRefName": "fix"}
        return []

    monkeypatch.setattr(prs, "_gh", fake_gh)
    db = Db(db_path(tmp_path), create=False)
    imp = prs.pr_impact(db, 7)
    db.close()
    assert imp["number"] == 7
    assert "api.py" in imp["affected_files"]
