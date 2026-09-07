"""Opt-in embedding tier — synonym retrieval and the re-extract cache."""

import math

import pytest

from codegraph import llm
from codegraph.config import db_path
from codegraph.db import Db
from codegraph.embed import build, nearest
from codegraph.pipeline import extract
from codegraph.query import query

# a tiny concept space: texts sharing a concept get a near-parallel vector
_CONCEPTS = {
    "auth": ["auth", "authenticate", "authentication", "login", "logon",
             "credential", "session", "signin"],
    "store": ["store", "database", "persist", "save", "repository", "user", "find"],
    "http": ["http", "request", "handler", "route", "endpoint", "api"],
}


def _mock_vec(text: str) -> list[float]:
    t = text.lower()
    v = [0.01, 0.01, 0.01]
    for i, (_, words) in enumerate(_CONCEPTS.items()):
        if any(w in t for w in words):
            v[i] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    llm.set_embed_mock(_mock_vec)
    yield
    llm.set_embed_mock(None)


@pytest.fixture
def proj(tmp_path):
    (tmp_path / "auth.py").write_text(
        "def login(user, pw):\n"
        "    return _check(user, pw)\n\n"
        "def _check(user, pw):\n"
        "    return True\n"
    )
    (tmp_path / "store.py").write_text(
        "def find_user(name):\n    return {}\n"
    )
    extract(tmp_path, semantic="none")
    return tmp_path


def test_embedding_bridges_synonym_gap(proj):
    db = Db(db_path(proj), create=False)
    backend = llm.make_embed_backend("mock")
    stats = build(db, backend)
    assert stats["stored"] > 0

    hits = nearest(db, backend, "authentication", k=3)
    labels = [
        db.conn.execute("SELECT label FROM nodes WHERE id=?", (nid,)).fetchone()["label"]
        for nid, _ in hits
    ]
    assert any("login" in l for l in labels)

    out = query(db, "how does authentication work")
    db.close()
    assert "login" in out


def test_vec_cache_survives_reextract(proj):
    db = Db(db_path(proj), create=False)
    backend = llm.make_embed_backend("mock")
    build(db, backend)
    cached = db.conn.execute("SELECT COUNT(*) n FROM vec_cache").fetchone()["n"]
    db.close()
    assert cached > 0

    extract(proj, force=True, semantic="none")  # reassigns every node PK

    db = Db(db_path(proj), create=False)
    stats = build(db, backend)
    db.close()
    assert stats["api_calls"] == 0        # everything served from vec_cache
    assert stats["stored"] > 0
