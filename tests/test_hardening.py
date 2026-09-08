"""PLAN §8 robustness ports: fd suppression, atomic replace, long paths,
backup-on-write, discrete INFERRED confidence."""

import os
import sys

import pytest

from codegraph._util import atomic_replace, long_path, suppressed_fds
from codegraph.config import out_dir
from codegraph.db import quantize_confidence
from codegraph.pipeline import extract


def test_suppressed_fds_silences_native_writes(capfd):
    print("before")
    with suppressed_fds():
        os.write(1, b"this should vanish\n")
        os.write(2, b"and this\n")
    print("after")
    out, err = capfd.readouterr()
    assert "this should vanish" not in out
    assert "before" in out and "after" in out


def test_atomic_replace_moves_file(tmp_path):
    src = tmp_path / "a.tmp"
    dst = tmp_path / "a.json"
    src.write_text("payload")
    atomic_replace(src, dst)
    assert dst.read_text() == "payload"
    assert not src.exists()


def test_long_path_is_noop_off_windows_or_prefixes_on_windows(tmp_path):
    got = long_path(tmp_path / "x")
    if os.name == "nt":
        assert got.startswith("\\\\?\\")
    else:
        assert got == str(tmp_path / "x")
    assert long_path("\\\\?\\C:\\already") == "\\\\?\\C:\\already"


def test_quantize_confidence_snaps_to_ladder():
    assert quantize_confidence(0.5) == 0.55
    assert quantize_confidence(0.7) == 0.65 or quantize_confidence(0.7) == 0.75
    assert quantize_confidence(0.99) == 0.95
    for v in (0.55, 0.65, 0.75, 0.85, 0.95):
        assert quantize_confidence(v) == v


def test_inferred_edges_are_on_the_ladder(tmp_path):
    (tmp_path / "store.py").write_text("def widget():\n    return 1\n")
    (tmp_path / "app.py").write_text("def run():\n    return widget()\n")  # no import
    extract(tmp_path, semantic="none", scip="none")
    from codegraph.config import db_path
    from codegraph.db import Db

    db = Db(db_path(tmp_path), create=False)
    inferred = [e["confidence_score"] for e in db.edges() if e["confidence"] == "INFERRED"]
    db.close()
    assert inferred
    assert all(s in (0.55, 0.65, 0.75, 0.85, 0.95) for s in inferred)


def test_extract_recovers_from_a_schema_bump(tmp_path):
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    extract(tmp_path, semantic="none", scip="none")

    from codegraph.config import db_path
    from codegraph.db import Db

    db = Db(db_path(tmp_path), create=False)
    db.set_meta("schema_version", "0")  # simulate a graph from an older codegraph
    db.conn.commit()
    db.close()

    # must not dead-end with "re-run extract --force" — it should just rebuild
    st = extract(tmp_path, semantic="none", scip="none")
    assert st["nodes"] >= 2


def test_backup_on_write_when_graph_is_protected(tmp_path, monkeypatch):
    (tmp_path / "a.py").write_text("def f():\n    return g()\n\ndef g():\n    return 1\n")

    # first build with a mock LLM so nodes get a rationale -> "protected"
    from codegraph import llm

    llm.set_mock(lambda s, u: '{"annotations":[{"label":"f","line":1,'
                 '"rationale":"entry point","concepts":["x"]}]}')
    extract(tmp_path, semantic="mock", scip="none")
    llm.set_mock(None)

    from datetime import date

    dated = out_dir(tmp_path) / date.today().isoformat()
    assert not dated.exists()

    # a second build must snapshot the prior artifacts first
    st = extract(tmp_path, force=True, semantic="none", scip="none")
    assert st["backup"] is not None
    assert (dated / "graph.json").exists()
