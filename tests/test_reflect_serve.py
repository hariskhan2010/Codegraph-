import io
import json

from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.pipeline import extract
from codegraph.reflect import reflect, save_result
from codegraph.serve import _dispatch


def _proj(tmp_path):
    (tmp_path / "auth.py").write_text(
        "def login(u):\n    return check(u)\n\ndef check(u):\n    return True\n"
    )
    extract(tmp_path, semantic="none")
    return Db(db_path(tmp_path), create=False)


def test_reflect_preferred_and_dead_end(tmp_path):
    db = _proj(tmp_path)
    save_result(db, "how to log in", "call login()", outcome="useful",
                source_nodes=["login()"])
    save_result(db, "how to log in again", "login()", outcome="useful",
                source_nodes=["login()"])
    save_result(db, "where is billing", "no idea", outcome="dead_end",
                source_nodes=["check()"])
    r = reflect(db)
    pref = {p["node"] for p in r["preferred"]}
    dead = {d.get("node") for d in r["dead_ends"]}
    assert "login()" in pref
    assert "check()" in dead
    # surfaced in the report
    from codegraph.render.report import write_report

    write_report(db, out_dir(tmp_path))
    md = (out_dir(tmp_path) / "GRAPH_REPORT.md").read_text()
    assert "Work-memory lessons" in md and "login()" in md
    db.close()


def test_serve_dispatch(tmp_path):
    db = _proj(tmp_path)
    out = _dispatch(db, "graph_stats", {})
    assert json.loads(out)["nodes"] > 0
    q = _dispatch(db, "query_graph", {"question": "login"})
    assert "login" in q
    aff = _dispatch(db, "affected", {"label": "check"})
    assert "login" in aff
    ctx = _dispatch(db, "get_context", {"label": "login"})
    assert "definition" in ctx and "check" in ctx
    db.close()


def test_serve_http_transport(tmp_path):
    import threading
    import urllib.request

    (tmp_path / "auth.py").write_text(
        "def login(u):\n    return check(u)\n\ndef check(u):\n    return True\n"
    )
    extract(tmp_path, semantic="none")

    from codegraph.serve import serve_http

    port = 8791
    t = threading.Thread(
        target=serve_http, args=(tmp_path,), kwargs={"port": port}, daemon=True
    )
    t.start()

    def rpc(payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        for _ in range(50):
            try:
                with urllib.request.urlopen(req, timeout=2) as r:
                    return json.loads(r.read())
            except Exception:
                import time

                time.sleep(0.1)
        raise AssertionError("server never came up")

    init = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["serverInfo"]["name"] == "codegraph"
    tools = rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert any(x["name"] == "query_graph" for x in tools["result"]["tools"])
    call = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "query_graph", "arguments": {"question": "login"}}})
    assert "login" in call["result"]["content"][0]["text"]


def test_serve_http_sse_mode(tmp_path):
    import threading
    import urllib.request

    (tmp_path / "auth.py").write_text("def login(u):\n    return 1\n")
    extract(tmp_path, semantic="none")

    from codegraph.serve import serve_http

    port = 8792
    threading.Thread(target=serve_http, args=(tmp_path,), kwargs={"port": port},
                     daemon=True).start()

    def sse_rpc(payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/mcp",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"},
        )
        for _ in range(50):
            try:
                with urllib.request.urlopen(req, timeout=2) as r:
                    return r.headers.get("Content-Type"), r.read().decode()
            except Exception:
                import time

                time.sleep(0.1)
        raise AssertionError("server never came up")

    ctype, body = sse_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "text/event-stream" in ctype
    assert body.startswith("event: message")
    data = json.loads(body.split("data: ", 1)[1].strip())
    assert any(t["name"] == "query_graph" for t in data["result"]["tools"])
