"""LSP resolver tier — precise call edges from a running language server.

Like the SCIP tier but live: codegraph starts the language server for each
language present in the graph, asks ``textDocument/references`` for every
definition node, and turns each reference site into an ``EXTRACTED``
``evidence='lsp'`` edge (caller = the node enclosing the reference).

Opt-in (``extract --lsp``) because it is O(defs) round-trips to an external
process. Auto-detects the server binary; languages with no server on PATH are
skipped. No dependency — JSON-RPC with ``Content-Length`` framing over pipes.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from .db import Db

# language family -> server argv (first hit on PATH wins)
SERVERS: dict[str, list[list[str]]] = {
    "python": [["pylsp"], ["pyright-langserver", "--stdio"], ["jedi-language-server"]],
    "jsts": [["typescript-language-server", "--stdio"]],
    "go": [["gopls"]],
    "rust": [["rust-analyzer"]],
    "native": [["clangd", "--log=error"]],
    "ruby": [["solargraph", "stdio"]],
    "jvm": [["jdtls"]],
}

_LANG_ID = {"python": "python", "jsts": "typescriptreact", "go": "go",
            "rust": "rust", "native": "cpp", "ruby": "ruby", "jvm": "java"}


class LspClient:
    """Minimal synchronous JSON-RPC client for an LSP server over stdio."""

    def __init__(self, argv: list[str], cwd: Path):
        self._p = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=str(cwd),
        )
        self._id = 0
        self._resp: dict[int, dict] = {}
        self._lock = threading.Condition()
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        buf = b""
        stdout = self._p.stdout
        assert stdout is not None
        while self._alive:
            header = b""
            while b"\r\n\r\n" not in header:
                chunk = stdout.read(1)
                if not chunk:
                    self._alive = False
                    return
                header += chunk
            length = 0
            for line in header.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":")[1].strip())
            body = b""
            while len(body) < length:
                more = stdout.read(length - len(body))
                if not more:
                    self._alive = False
                    return
                body += more
            try:
                msg = json.loads(body)
            except json.JSONDecodeError:
                continue
            if "id" in msg and ("result" in msg or "error" in msg):
                with self._lock:
                    self._resp[msg["id"]] = msg
                    self._lock.notify_all()

    def _send(self, obj: dict) -> None:
        data = json.dumps(obj).encode()
        assert self._p.stdin is not None
        self._p.stdin.write(f"Content-Length: {len(data)}\r\n\r\n".encode() + data)
        self._p.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict, *, timeout: float = 15.0) -> dict:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.time() + timeout
        with self._lock:
            while rid not in self._resp:
                if not self._alive or not self._lock.wait(deadline - time.time()):
                    raise TimeoutError(f"{method} timed out")
                if time.time() > deadline:
                    raise TimeoutError(f"{method} timed out")
            msg = self._resp.pop(rid)
        if "error" in msg:
            raise RuntimeError(msg["error"].get("message", "lsp error"))
        return msg.get("result")

    def shutdown(self) -> None:
        self._alive = False
        try:
            self._send({"jsonrpc": "2.0", "id": 999999, "method": "shutdown",
                        "params": None})
            self.notify("exit", {})
        except (OSError, ValueError):
            pass
        try:
            self._p.terminate()
            self._p.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self._p.kill()


def _find_server(family: str):
    import shutil

    for argv in SERVERS.get(family, []):
        if shutil.which(argv[0]):
            return argv
    return None


def _uri(p: Path) -> str:
    return p.resolve().as_uri()


def _parse_loc(loc: str | None) -> tuple[int, int]:
    if not loc:
        return 0, 0
    nums = [int(x) for x in loc.replace("L", " ").replace("-", " ").split() if x.isdigit()]
    return (nums[0], nums[1] if len(nums) > 1 else nums[0]) if nums else (0, 0)


def edges_from_references(db: Db, refs_by_node: dict[int, list[tuple[str, int]]]) -> int:
    """``refs_by_node``: def-node id -> list of (relative_file, 1-based line) where
    it is referenced. Build the enclosing-caller edges. Shared with the tests so
    the graph logic is exercised without a live server."""
    per_file: dict[str, list[tuple[int, int, int, str]]] = {}
    for r in db.nodes():
        if not r["source_file"]:
            continue
        a, b = _parse_loc(r["source_location"])
        per_file.setdefault(r["source_file"], []).append(
            (a, b, int(r["id"]), r["kind"] or ""))

    def enclosing(rel: str, line: int) -> tuple[int, str] | None:
        best, best_start = None, -1
        for a, b, nid, kind in per_file.get(rel, []):
            if a <= line <= b and a > best_start:
                best, best_start = (nid, kind), a
        return best

    n = 0
    with db.tx() as c:
        c.execute("DELETE FROM edges WHERE evidence='lsp'")
        for dst_id, refs in refs_by_node.items():
            dst_kind = next((k for spans in per_file.values()
                             for a, b, i, k in spans if i == dst_id), "")
            rel_name = "calls" if dst_kind in ("function", "method") else "references"
            for rel, line in refs:
                src = enclosing(rel, line)
                if src is None or src[0] == dst_id:
                    continue
                cur = c.execute(
                    "INSERT OR IGNORE INTO edges(src,dst,relation,confidence,"
                    "confidence_score,context,source_file,source_location,weight,evidence)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (src[0], dst_id, rel_name, "EXTRACTED", 1.0,
                     "call" if rel_name == "calls" else "reference",
                     rel, f"L{line}", 1.0, "lsp"),
                )
                if cur.rowcount and cur.rowcount > 0:
                    n += 1
        db.set_meta("resolver_tier", "lsp")
    return n


def resolve(db: Db, root: Path, *, families: list[str] | None = None,
            max_defs: int = 4000, timeout: float = 15.0, progress=None) -> dict:
    root = Path(root)
    nodes = db.nodes()
    by_id = {int(r["id"]): r for r in nodes}
    lang_of_file: dict[str, str] = {
        r["source_file"]: (r["file_type"] or "") for r in nodes
    }
    fam_of_file: dict[str, str] = {}
    for r in db.conn.execute("SELECT path, lang FROM files WHERE status='present'").fetchall():
        fam_of_file[r["path"]] = _FAMILY.get(r["lang"] or "", "")

    families = families or sorted({f for f in fam_of_file.values() if f})
    refs_by_node: dict[int, list[tuple[str, int]]] = {}
    used_families: list[str] = []
    errors: list[str] = []

    for fam in families:
        argv = _find_server(fam)
        if not argv:
            continue
        files = sorted({f for f, ff in fam_of_file.items() if ff == fam})
        defs = [r for r in nodes if r["source_file"] in files
                and (r["kind"] or "") in ("function", "method", "class")][:max_defs]
        if not defs:
            continue
        try:
            client = LspClient(argv, root)
            client.request("initialize", {
                "processId": None, "rootUri": _uri(root),
                "capabilities": {}, "workspaceFolders": [
                    {"uri": _uri(root), "name": root.name}],
            }, timeout=timeout)
            client.notify("initialized", {})
        except (OSError, TimeoutError, RuntimeError) as e:
            errors.append(f"{fam}: {e}")
            continue

        opened: set[str] = set()

        def _open(rel: str) -> bool:
            fp = root / rel
            try:
                txt = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return False
            client.notify("textDocument/didOpen", {"textDocument": {
                "uri": _uri(fp), "languageId": _LANG_ID.get(fam, "plaintext"),
                "version": 1, "text": txt}})
            opened.add(rel)
            return True

        for r in defs:
            rel = r["source_file"]
            if rel not in opened and not _open(rel):
                continue
            line0 = _parse_loc(r["source_location"])[0] - 1
            name = (r["label"] or "").strip(".()")
            try:
                src_line = (root / rel).read_text(
                    encoding="utf-8", errors="replace").splitlines()[line0]
                col = max(src_line.find(name), 0)
            except (OSError, IndexError):
                col = 0
            try:
                locs = client.request("textDocument/references", {
                    "textDocument": {"uri": _uri(root / rel)},
                    "position": {"line": line0, "character": col},
                    "context": {"includeDeclaration": False},
                }, timeout=timeout) or []
            except (TimeoutError, RuntimeError):
                continue
            hits: list[tuple[str, int]] = []
            for loc in locs:
                luri = loc.get("uri", "")
                try:
                    lp = Path(_from_uri(luri)).resolve().relative_to(root.resolve())
                except (ValueError, OSError):
                    continue
                hits.append((lp.as_posix(),
                             loc.get("range", {}).get("start", {}).get("line", 0) + 1))
            if hits:
                refs_by_node[int(r["id"])] = hits

        client.shutdown()
        used_families.append(fam)
        if progress:
            progress({"lsp_family": fam, "defs": len(defs)})

    edges = edges_from_references(db, refs_by_node) if refs_by_node else 0
    return {"families": used_families, "edges": edges, "errors": errors,
            "defs_probed": len(refs_by_node)}


def _from_uri(uri: str) -> str:
    from urllib.parse import unquote, urlparse

    p = urlparse(uri)
    path = unquote(p.path)
    if path.startswith("/") and len(path) > 2 and path[2] == ":":  # /C:/…
        path = path[1:]
    return path


_FAMILY = {
    "python": "python", "javascript": "jsts", "typescript": "jsts",
    "go": "go", "rust": "rust", "c": "native", "cpp": "native",
    "ruby": "ruby", "java": "jvm",
}
