"""`codegraph add` — external document ingestion into the graph."""

import pytest

from codegraph import ingest
from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.pipeline import extract, update
from codegraph.query import query


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "code.py").write_text("def run():\n    return 1\n")
    extract(tmp_path, semantic="none")
    return tmp_path


def test_add_local_markdown_is_queryable(proj):
    doc = proj / "notes.md"
    doc.write_text("# RFC 42\n## Wire protocol\nframes are length-prefixed\n")
    r = ingest.add(proj, str(doc))
    assert r["sections"] == 2

    db = Db(db_path(proj), create=False)
    kinds = {n["kind"] for n in db.nodes()}
    assert "section" in kinds
    out = query(db, "wire protocol")
    db.close()
    assert "Wire protocol" in out


def test_add_arxiv_uses_api(proj, monkeypatch):
    atom = (
        "<feed><entry><title>Attention Is All You Need</title>"
        "<summary>We propose the Transformer.</summary>"
        "<name>A. Vaswani</name></entry></feed>"
    )
    monkeypatch.setattr(ingest, "_get", lambda url, **k: atom.encode())
    r = ingest.add(proj, "https://arxiv.org/abs/1706.03762")
    assert "Attention Is All You Need" in r["title"]
    md = (out_dir(proj) / "sources" / (list((out_dir(proj) / "sources").glob("*.md"))[0].name))
    assert "Transformer" in md.read_text()


def test_ingested_source_survives_update(proj):
    doc = proj / "spec.md"
    doc.write_text("# Spec\n## Section A\ntext here\n")
    ingest.add(proj, str(doc))
    rel = f"{out_dir(proj).name}/sources/spec.md"

    db = Db(db_path(proj), create=False)
    assert any(f["path"] == rel for f in db.all_files())
    db.close()

    (proj / "code.py").write_text("def run():\n    return 2\n")  # unrelated change
    update(proj)

    db = Db(db_path(proj), create=False)
    files = {f["path"]: f["status"] for f in db.all_files(status=None)}
    db.close()
    assert files.get(rel) == "present"          # not pruned


def test_add_unknown_extension_errors(proj):
    bad = proj / "thing.xyz"
    bad.write_text("x")
    with pytest.raises(ingest.IngestError):
        ingest.add(proj, str(bad))


def test_add_notion_page(proj, monkeypatch):
    monkeypatch.setenv("NOTION_TOKEN", "secret")

    def fake(url, headers, **k):
        if "/pages/" in url:
            return {"properties": {"Name": {"type": "title",
                    "title": [{"plain_text": "Design Doc"}]}}}
        if "/children" in url and "start_cursor" not in url:
            return {"results": [
                {"id": "b1", "type": "heading_1",
                 "heading_1": {"rich_text": [{"plain_text": "Goals"}]},
                 "has_children": False},
                {"id": "b2", "type": "paragraph",
                 "paragraph": {"rich_text": [{"plain_text": "ship it"}]},
                 "has_children": False},
            ], "has_more": False}
        return {"results": [], "has_more": False}

    monkeypatch.setattr(ingest, "_get_json", fake)
    r = ingest.add(proj, "notion:abc123")
    assert r["title"] == "Design Doc"
    db = Db(db_path(proj), create=False)
    labels = {n["label"] for n in db.nodes()}
    db.close()
    assert "Goals" in labels


def test_add_notion_without_token_errors(proj, monkeypatch):
    monkeypatch.delenv("NOTION_TOKEN", raising=False)
    monkeypatch.delenv("NOTION_API_KEY", raising=False)
    with pytest.raises(ingest.IngestError, match="NOTION_TOKEN"):
        ingest.add(proj, "notion:abc123")
