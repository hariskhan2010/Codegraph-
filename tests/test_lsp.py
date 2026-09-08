"""LSP resolver tier — protocol framing + reference->edge logic.

A live language server isn't assumed in CI: the graph logic is tested directly
via ``edges_from_references``, the JSON-RPC framing via a tiny echo server, and
``resolve`` is asserted to no-op cleanly when no server binary is present.
"""

import sys
import textwrap

import pytest

from codegraph.config import db_path
from codegraph.db import Db
from codegraph.lsp import LspClient, edges_from_references, resolve
from codegraph.pipeline import extract


@pytest.fixture
def proj(tmp_path):
    # two `save()` — the name-based guard can't resolve app.run()'s call
    (tmp_path / "store.py").write_text("def save(x):\n    return x\n")
    (tmp_path / "cache.py").write_text("def save(x):\n    return x\n")
    (tmp_path / "app.py").write_text(
        "def run():\n    save(1)\n    return 2\n"
    )
    extract(tmp_path, semantic="none", scip="none")
    return tmp_path


def test_edges_from_references_builds_caller_edges(proj):
    db = Db(db_path(proj), create=False)
    store_save = next(int(r["id"]) for r in db.nodes()
                      if r["label"] == "save()" and r["source_file"] == "store.py")
    # the server says store.save is referenced at app.py:2 (0-based line 1 -> 2)
    n = edges_from_references(db, {store_save: [("app.py", 2)]})
    assert n == 1
    by_id = {int(r["id"]): r for r in db.nodes()}
    lsp_edges = {
        (by_id[int(e["src"])]["label"], by_id[int(e["dst"])]["source_file"])
        for e in db.edges() if e["evidence"] == "lsp"
    }
    db.close()
    assert ("run()", "store.py") in lsp_edges          # disambiguated
    assert ("run()", "cache.py") not in lsp_edges


def test_edges_from_references_is_idempotent(proj):
    db = Db(db_path(proj), create=False)
    sid = next(int(r["id"]) for r in db.nodes()
               if r["label"] == "save()" and r["source_file"] == "store.py")
    edges_from_references(db, {sid: [("app.py", 2)]})
    a = len([e for e in db.edges() if e["evidence"] == "lsp"])
    edges_from_references(db, {sid: [("app.py", 2)]})
    b = len([e for e in db.edges() if e["evidence"] == "lsp"])
    db.close()
    assert a == b == 1


def test_resolve_noops_without_a_server(proj, monkeypatch):
    import codegraph.lsp as lspmod

    monkeypatch.setattr(lspmod, "_find_server", lambda fam: None)
    db = Db(db_path(proj), create=False)
    r = resolve(db, proj)
    db.close()
    assert r["edges"] == 0 and r["families"] == []


_ECHO_SERVER = textwrap.dedent('''
    import json, sys
    def read():
        h = b""
        while b"\\r\\n\\r\\n" not in h:
            h += sys.stdin.buffer.read(1)
        n = int([l for l in h.split(b"\\r\\n") if l.lower().startswith(b"content-length:")][0].split(b":")[1])
        return json.loads(sys.stdin.buffer.read(n))
    def write(o):
        b = json.dumps(o).encode()
        sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(b) + b)
        sys.stdout.buffer.flush()
    while True:
        try:
            msg = read()
        except Exception:
            break
        if "id" in msg:
            write({"jsonrpc": "2.0", "id": msg["id"], "result": {"echo": msg.get("method")}})
        if msg.get("method") == "exit":
            break
''')


@pytest.mark.skipif(not __import__("shutil").which("pylsp"),
                    reason="python-lsp-server not installed")
def test_lsp_resolve_disambiguates_with_real_server(tmp_path):
    (tmp_path / "store.py").write_text("def persist(x):\n    return x\n")
    (tmp_path / "cache.py").write_text("def persist(x):\n    return x\n")
    # star import: `persist` is in scope but the import table can't name the
    # module — the ambiguity only a real language server resolves.
    (tmp_path / "app.py").write_text("from store import *\n\n"
                                     "def run():\n    return persist(1)\n")
    extract(tmp_path, semantic="none", scip="none")

    db = Db(db_path(tmp_path), create=False)
    pre = {(r["source_file"]) for r in db.nodes() if r["label"] == "persist()"}
    assert pre == {"store.py", "cache.py"}
    r = resolve(db, tmp_path)
    by_id = {int(x["id"]): x for x in db.nodes()}
    lsp = {(by_id[int(e["src"])]["label"], by_id[int(e["dst"])]["source_file"])
           for e in db.edges() if e["evidence"] == "lsp"}
    db.close()
    assert r["edges"] >= 1
    assert ("run()", "store.py") in lsp
    assert ("run()", "cache.py") not in lsp


def test_lsp_client_framing(tmp_path):
    script = tmp_path / "echo_lsp.py"
    script.write_text(_ECHO_SERVER)
    client = LspClient([sys.executable, str(script)], tmp_path)
    try:
        res = client.request("initialize", {"rootUri": None}, timeout=10)
        assert res == {"echo": "initialize"}
        res = client.request("textDocument/references", {}, timeout=10)
        assert res == {"echo": "textDocument/references"}
    finally:
        client.shutdown()
