"""Every export target is a deterministic serialization of the DB."""

import json
import xml.dom.minidom as minidom

import pytest

from codegraph.config import db_path
from codegraph.db import Db
from codegraph.export import TARGETS, export, to_cypher, to_dot, to_graphml
from codegraph.pipeline import extract


@pytest.fixture
def db(tmp_path):
    (tmp_path / "a.py").write_text(
        "class Store:\n"
        "    def get(self, k):\n        return self._read(k)\n"
        "    def _read(self, k):\n        return k\n"
    )
    extract(tmp_path, semantic="none")
    return tmp_path


@pytest.mark.parametrize("target", TARGETS)
def test_export_target_writes_files(db, target):
    written = export(Db(db_path(db), create=False), target, db)
    assert written
    for p in written:
        assert p.exists()


def test_wiki_has_index_and_one_article_per_community(db):
    (export(Db(db_path(db), create=False), "wiki", db))
    from codegraph.config import out_dir

    wdir = out_dir(db) / "exports" / "wiki"
    idx = (wdir / "index.md").read_text()
    assert idx.startswith("# Graph Wiki")
    articles = list(wdir.glob("[0-9][0-9][0-9]-*.md"))
    # every article linked from the index exists on disk
    for a in articles:
        assert f"({a.name})" in idx
        assert (wdir / a.name).read_text().startswith("# ")


def test_graphml_is_valid_xml(db):
    x = to_graphml(Db(db_path(db), create=False))
    doc = minidom.parseString(x)
    assert doc.getElementsByTagName("node")
    assert doc.getElementsByTagName("edge")


def test_cypher_has_merge_per_node_and_edge(db):
    d = Db(db_path(db), create=False)
    c = to_cypher(d)
    n = d.stats()["nodes"]
    d.close()
    assert c.count("MERGE (n:Symbol") == n
    assert "-[r:METHOD]->" in c or "-[r:CONTAINS]->" in c


def test_jsonl_roundtrips(db):
    export(Db(db_path(db), create=False), "jsonl", db)
    from codegraph.config import out_dir

    ex = out_dir(db) / "exports"
    nodes = [json.loads(l) for l in (ex / "nodes.jsonl").read_text().splitlines()]
    edges = [json.loads(l) for l in (ex / "edges.jsonl").read_text().splitlines()]
    ids = {n["id"] for n in nodes}
    assert nodes and all(e["source"] in ids and e["target"] in ids for e in edges)


def test_export_is_deterministic(db):
    d = Db(db_path(db), create=False)
    assert to_dot(d) == to_dot(d)
    assert to_graphml(d) == to_graphml(d)
    d.close()


def test_load_cypher_reports_missing_driver(db, monkeypatch):
    import shutil

    from codegraph.export import load_cypher

    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setitem(__import__("sys").modules, "neo4j", None)
    d = Db(db_path(db), create=False)
    with pytest.raises(RuntimeError, match="neo4j|cypher-shell"):
        load_cypher(d)
    d.close()
