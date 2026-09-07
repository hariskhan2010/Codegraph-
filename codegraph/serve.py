"""MCP server (stdio) — graphify-compatible tool surface.

Line-delimited JSON-RPC 2.0 over stdin/stdout, no ``mcp`` package dependency.
Tool names and shapes match graphify so an existing agent config keeps working:
``query_graph``, ``get_node``, ``get_neighbors``, ``god_nodes``, ``graph_stats``,
``shortest_path``, plus codegraph's ``affected``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import __version__
from .config import db_path
from .db import Db

_TOOLS = [
    {
        "name": "query_graph",
        "description": "Answer a question from the code graph. Returns a bounded "
                       "subgraph (NODE/EDGE lines) seeded by the question.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "depth": {"type": "integer", "default": 2},
                "token_budget": {"type": "integer", "default": 2000},
            },
            "required": ["question"],
        },
    },
    {
        "name": "get_node",
        "description": "Describe one node and its direct connections.",
        "inputSchema": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        },
    },
    {
        "name": "get_neighbors",
        "description": "Incoming and outgoing edges of one node.",
        "inputSchema": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        },
    },
    {
        "name": "affected",
        "description": "Reverse-dependency traversal: what breaks if this symbol "
                       "changes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "depth": {"type": "integer", "default": 3},
            },
            "required": ["label"],
        },
    },
    {
        "name": "get_context",
        "description": "The code an agent needs for one symbol: its full body "
                       "plus caller/callee signatures, each with file:line.",
        "inputSchema": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
        },
    },
    {
        "name": "shortest_path",
        "description": "Shortest path between two symbols.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "target": {"type": "string"},
            },
            "required": ["source", "target"],
        },
    },
    {
        "name": "god_nodes",
        "description": "Most-connected nodes (core abstractions).",
        "inputSchema": {
            "type": "object",
            "properties": {"top_n": {"type": "integer", "default": 15}},
        },
    },
    {
        "name": "graph_stats",
        "description": "Node / edge / community counts and provenance breakdown.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_prs",
        "description": "Open pull requests ranked by graph blast radius (needs `gh`).",
        "inputSchema": {"type": "object", "properties": {
            "limit": {"type": "integer", "default": 30}}},
    },
    {
        "name": "get_pr_impact",
        "description": "What one PR can break: downstream files and nodes.",
        "inputSchema": {"type": "object", "properties": {
            "number": {"type": "integer"}}, "required": ["number"]},
    },
]


def _dispatch(db: Db, name: str, args: dict) -> str:
    from . import analyze, query

    if name == "query_graph":
        return query.query(db, args["question"],
                           depth=int(args.get("depth", 2)),
                           budget=int(args.get("token_budget", 2000)))
    if name == "get_node":
        return query.explain(db, args["label"])
    if name == "get_neighbors":
        return query.explain(db, args["label"])
    if name == "affected":
        return query.affected(db, args["label"], depth=int(args.get("depth", 3)))
    if name == "get_context":
        from .context import context

        return context(db, args["label"])
    if name == "shortest_path":
        return query.shortest_path(db, args["source"], args["target"])
    if name == "god_nodes":
        gs = analyze.god_nodes(db, int(args.get("top_n", 15)))
        return "\n".join(f"{i:2}. {g['label']}  ({g['degree']} edges)"
                         for i, g in enumerate(gs, 1))
    if name == "graph_stats":
        return json.dumps(db.stats(), indent=2)
    if name in ("list_prs", "get_pr_impact"):
        from . import prs

        try:
            if name == "list_prs":
                return json.dumps(prs.triage(db, int(args.get("limit", 30))), indent=2)
            return json.dumps(prs.pr_impact(db, int(args["number"])), indent=2)
        except prs.GhUnavailable as e:
            return f"PR tools need the GitHub CLI: {e}"
    raise ValueError(f"unknown tool {name}")


def _resolve_db(path: Path) -> Path:
    if path.is_file() and path.suffix == ".db":
        return path
    p = db_path(path)
    if not p.exists():
        raise SystemExit(f"no graph at {p} — run `codegraph extract {path}` first")
    return p


def handle(db: Db, req: dict) -> dict | None:
    """One JSON-RPC request -> one response dict (or ``None`` for a notification)."""
    mid = req.get("id")
    method = req.get("method")
    params = req.get("params") or {}

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "codegraph", "version": __version__},
        }}
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": _TOOLS}}
    if method == "tools/call":
        tname = params.get("name", "")
        targs = params.get("arguments") or {}
        try:
            text = _dispatch(db, tname, targs)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}]}}
        except Exception as e:  # never crash the loop
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": f"error: {e}"}], "isError": True}}
    if mid is not None:
        return {"jsonrpc": "2.0", "id": mid, "error": {
            "code": -32601, "message": f"method not found: {method}"}}
    return None


def serve(path: Path) -> None:
    """stdio transport (Claude Desktop, most MCP clients)."""
    db = Db(_resolve_db(Path(path)), create=False)
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(db, req)
        if resp is not None:
            out.write(json.dumps(resp) + "\n")
            out.flush()
    db.close()


def serve_http(path: Path, *, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Streamable-HTTP transport (MCP 2025-03-26).

    * ``POST /mcp`` — a JSON-RPC message (or batch). The reply is
      ``application/json`` by default, or an SSE stream (``event: message``) when
      the client's ``Accept`` header asks for ``text/event-stream``.
    * ``GET /mcp`` with ``Accept: text/event-stream`` — opens the server->client
      SSE channel (keep-alive comments; held open until the client disconnects).
    * ``DELETE /mcp`` — ends the session.
    * ``Mcp-Session-Id`` is minted on ``initialize`` and echoed thereafter.
    """
    import secrets
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    dbp = _resolve_db(Path(path))
    sessions: set[str] = set()
    lock = threading.Lock()

    def _wants_sse(accept: str) -> bool:
        return "text/event-stream" in (accept or "")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # -- helpers -------------------------------------------------------
        def _session(self) -> str | None:
            return self.headers.get("Mcp-Session-Id")

        def _send_json(self, code: int, payload, *, sid: str | None = None) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if sid:
                self.send_header("Mcp-Session-Id", sid)
            self.end_headers()
            self.wfile.write(body)

        def _send_sse_messages(self, msgs: list, *, sid: str | None = None) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            if sid:
                self.send_header("Mcp-Session-Id", sid)
            self.end_headers()
            for m in msgs:
                self.wfile.write(f"event: message\ndata: {json.dumps(m)}\n\n".encode())
            self.wfile.flush()

        def _bad_path(self) -> bool:
            if self.path.rstrip("/") not in ("", "/mcp"):
                self._send_json(404, {"error": "not found"})
                return True
            return False

        # -- verbs --------------------------------------------------------
        def do_GET(self):  # noqa: N802
            if self._bad_path():
                return
            if not _wants_sse(self.headers.get("Accept", "")):
                self.send_response(405)
                self.send_header("Allow", "POST, GET, DELETE")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            sid = self._session()
            if sid:
                self.send_header("Mcp-Session-Id", sid)
            self.end_headers()
            try:
                while True:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    time.sleep(15)
            except (BrokenPipeError, ConnectionResetError, OSError):
                return

        def do_DELETE(self):  # noqa: N802
            if self._bad_path():
                return
            sid = self._session()
            with lock:
                sessions.discard(sid or "")
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self):  # noqa: N802
            if self._bad_path():
                return
            n = int(self.headers.get("Content-Length", 0))
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                self._send_json(400, {"jsonrpc": "2.0", "id": None,
                                      "error": {"code": -32700, "message": "parse error"}})
                return

            reqs = req if isinstance(req, list) else [req]
            is_init = any(r.get("method") == "initialize" for r in reqs)
            sid = self._session()
            if is_init:
                sid = "cg-" + secrets.token_hex(8)
                with lock:
                    sessions.add(sid)

            db = Db(dbp, create=False)
            try:
                out = [r for r in (handle(db, one) for one in reqs) if r is not None]
            finally:
                db.close()

            if not out:
                self.send_response(202)
                if sid:
                    self.send_header("Mcp-Session-Id", sid)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return

            payload = out if isinstance(req, list) else out[0]
            if _wants_sse(self.headers.get("Accept", "")):
                self._send_sse_messages(out, sid=sid)
            else:
                self._send_json(200, payload, sid=sid)

        def log_message(self, *a):  # quiet
            pass

    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"codegraph MCP (Streamable HTTP) on http://{host}:{port}/mcp  (Ctrl-C to stop)",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
