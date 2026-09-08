"""SQLite store — the authoritative graph state.

Everything lives in one file (``codegraph-out/codegraph.db``, WAL mode).
``graph.json`` / ``GRAPH_REPORT.md`` / ``graph.html`` are rendered *outputs*
re-emitted from this database, never a second source of truth.

The design fix for graphify's incremental-update bug cluster is
:func:`Db.replace_file`: re-extracting one file is a single transaction that
deletes that file's rows and inserts the fresh ones. A crash rolls back. There
is no manifest diffing, no ``_infer_merge_root`` guessing, no prune-set
derivation, and no shrink guard — the store is transactional, so it cannot be
left half-written.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from . import SCHEMA_VERSION
from .config import nfc

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,   -- root-relative, forward-slash, NFC
    abs_identity  TEXT,               -- canonical absolute posix, for prune matching
    file_type     TEXT,               -- code|document|paper|image|video
    lang          TEXT,
    content_sha256 TEXT,
    ast_hash      TEXT DEFAULT '',    -- '' => needs AST (re)extraction
    semantic_hash TEXT DEFAULT '',    -- '' => needs semantic (re)extraction
    mtime         REAL,
    seen          REAL,
    status        TEXT DEFAULT 'present'  -- present|excluded|deleted
);

CREATE TABLE IF NOT EXISTS nodes (
    id            INTEGER PRIMARY KEY,
    slug          TEXT,               -- graphify-compatible; NOT a unique key
    label         TEXT NOT NULL,
    norm_label    TEXT,
    file_type     TEXT,
    source_file   TEXT,
    source_location TEXT,
    definition_file TEXT,
    definition_location TEXT,
    kind          TEXT,               -- function|method|class|interface|namespace|module|file|concept|stub
    origin        TEXT,               -- ast|semantic|stub
    rationale     TEXT,
    community     INTEGER,
    community_name TEXT,
    degree        INTEGER DEFAULT 0,
    extra         TEXT                -- JSON: author, contributor, captured_at, metadata, verification, ...
);
CREATE INDEX IF NOT EXISTS ix_nodes_source_file ON nodes(source_file);
CREATE INDEX IF NOT EXISTS ix_nodes_slug        ON nodes(slug);
CREATE INDEX IF NOT EXISTS ix_nodes_norm_label  ON nodes(norm_label);

CREATE TABLE IF NOT EXISTS edges (
    id            INTEGER PRIMARY KEY,
    src           INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    dst           INTEGER NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    relation      TEXT NOT NULL,
    confidence    TEXT,               -- EXTRACTED|INFERRED|AMBIGUOUS
    confidence_score REAL,
    context       TEXT,
    source_file   TEXT,               -- the relation *site*
    source_location TEXT,
    weight        REAL DEFAULT 1.0,
    evidence      TEXT,               -- import|scip|stack-graphs|same-file|proximity|llm
    deferred      INTEGER DEFAULT 0,
    type_only     INTEGER DEFAULT 0,
    UNIQUE(src, dst, relation, source_file, source_location)
);
CREATE INDEX IF NOT EXISTS ix_edges_src ON edges(src);
CREATE INDEX IF NOT EXISTS ix_edges_dst ON edges(dst);
CREATE INDEX IF NOT EXISTS ix_edges_src_file ON edges(source_file);

CREATE TABLE IF NOT EXISTS hyperedges (
    id            INTEGER PRIMARY KEY,
    slug          TEXT,
    label         TEXT,
    relation      TEXT,
    confidence    TEXT,
    confidence_score REAL,
    source_file   TEXT
);
CREATE TABLE IF NOT EXISTS hyperedge_members (
    hyperedge_id  INTEGER REFERENCES hyperedges(id) ON DELETE CASCADE,
    node_id       INTEGER REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS raw_refs (
    caller_id       INTEGER REFERENCES nodes(id) ON DELETE CASCADE,
    callee          TEXT,
    relation        TEXT DEFAULT 'calls',   -- calls|inherits|implements|imports|references
    source_file     TEXT,
    source_location TEXT,
    import_evidence INTEGER DEFAULT 0,
    lang            TEXT,
    is_method       INTEGER DEFAULT 0,      -- receiver present at the call site (x.foo())
    recv            TEXT,                   -- receiver token ('self', a var name, ...)
    recv_kind       TEXT,                   -- bare|self|self_attr|name|ctor|complex|dotted
    recv_type       TEXT,                   -- inferred class of the receiver, if known
    caller_class    TEXT,                   -- enclosing class of the call site, if any
    import_module   TEXT,                   -- posix path fragment the callee is imported from
    import_symbol   TEXT                    -- the symbol name in that module
);
CREATE INDEX IF NOT EXISTS ix_rawrefs_file ON raw_refs(source_file);
CREATE INDEX IF NOT EXISTS ix_rawrefs_callee ON raw_refs(callee);

CREATE TABLE IF NOT EXISTS node_trigrams (
    trigram  TEXT,
    node_id  INTEGER REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_trigram ON node_trigrams(trigram);
CREATE INDEX IF NOT EXISTS ix_trigram_node ON node_trigrams(node_id);

CREATE TABLE IF NOT EXISTS node_vec (
    node_id   INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    dim       INTEGER,
    embedding BLOB
);
-- survives delete+reinsert of nodes, so `embed` re-runs cost no API calls
CREATE TABLE IF NOT EXISTS vec_cache (
    text_sha  TEXT PRIMARY KEY,
    model     TEXT,
    dim       INTEGER,
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS communities (
    id        INTEGER PRIMARY KEY,
    label     TEXT,
    cohesion  REAL,
    member_sig TEXT
);

CREATE TABLE IF NOT EXISTS queries (
    ts            REAL,
    kind          TEXT,
    question      TEXT,
    corpus        TEXT,
    nodes_returned INTEGER,
    duration_ms   REAL,
    outcome       TEXT,
    correction    TEXT,
    source_nodes  TEXT
);
"""

# Contentless FTS5 — populated / deleted alongside nodes, rowid == nodes.id.
_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    label, slug, source_file, rationale,
    tokenize = 'porter unicode61 remove_diacritics 2',
    content = ''
);
"""


def _trigrams(text: str) -> set[str]:
    t = text.lower()
    if len(t) < 3:
        return {t} if t else set()
    return {t[i : i + 3] for i in range(len(t) - 2)}


class Db:
    """Thin wrapper around a SQLite connection with the codegraph schema."""

    def __init__(self, path: Path | str, *, create: bool = True):
        self.path = Path(path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        elif not self.path.exists():
            raise FileNotFoundError(f"no codegraph database at {self.path}")
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.has_fts = self._init_fts()
        try:
            self._init_schema()
        except Exception:
            self.conn.close()  # don't leak the handle (Windows can't unlink it)
            raise

    # -- lifecycle -----------------------------------------------------------

    def _init_fts(self) -> bool:
        try:
            self.conn.executescript(_FTS_SCHEMA)
            return True
        except sqlite3.OperationalError:
            return False  # SQLite built without FTS5 — query.py degrades gracefully

    def _init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        cur = self.get_meta("schema_version")
        if cur is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))
        elif int(cur) != SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema v{cur}, this codegraph expects v{SCHEMA_VERSION}; "
                "re-run `codegraph extract --force`"
            )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Db":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """One atomic transaction. Rolls back on any exception."""
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # -- meta --------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    # -- files -----------------------------------------------------------

    def upsert_file(self, **f: Any) -> None:
        cols = (
            "path", "abs_identity", "file_type", "lang", "content_sha256",
            "ast_hash", "semantic_hash", "mtime", "seen", "status",
        )
        vals = {c: f.get(c) for c in cols}
        vals["path"] = nfc(vals["path"])
        present = [c for c in cols if vals[c] is not None]
        self.conn.execute(
            f"INSERT INTO files({','.join(present)}) VALUES({','.join('?' for _ in present)}) "
            f"ON CONFLICT(path) DO UPDATE SET "
            + ",".join(f"{c}=excluded.{c}" for c in present if c != "path"),
            [vals[c] for c in present],
        )

    def file_row(self, path: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM files WHERE path=?", (nfc(path),)
        ).fetchone()

    def all_files(self, *, status: str | None = "present") -> list[sqlite3.Row]:
        if status is None:
            return self.conn.execute("SELECT * FROM files").fetchall()
        return self.conn.execute(
            "SELECT * FROM files WHERE status=?", (status,)
        ).fetchall()

    # -- nodes / edges: bulk replacement for one source file ---------------

    def replace_file(
        self,
        source_file: str,
        nodes: Sequence[dict],
        edges: Sequence[dict],
        *,
        raw_calls: Sequence[dict] = (),
        origin: str = "ast",
    ) -> dict[str, int]:
        """Atomically swap all ``origin`` rows attributed to ``source_file``.

        ``nodes`` items: slug,label,kind,source_location,file_type,rationale,extra,...
        ``edges`` items: src_slug,dst_slug,relation,confidence,confidence_score,
                         context,source_location,evidence,deferred,type_only

        Edge endpoints are given as slugs and resolved to node ids: first against
        the nodes inserted in this call, then against existing nodes by slug.
        Endpoints that resolve to nothing are dropped (dangling external refs),
        matching graphify's ``build_from_json`` edge loop.
        """
        source_file = nfc(source_file)
        with self.tx() as c:
            if origin == "ast":
                c.execute(
                    "DELETE FROM nodes WHERE source_file=? AND origin IN ('ast','stub')",
                    (source_file,),
                )
            else:
                c.execute(
                    "DELETE FROM nodes WHERE source_file=? AND origin=?",
                    (source_file, origin),
                )
            # edges dst/src cascade-deleted with their nodes; also clear edges
            # whose *site* is this file but whose endpoints live elsewhere.
            c.execute("DELETE FROM edges WHERE source_file=?", (source_file,))

            slug_to_id: dict[str, int] = {}
            for n in nodes:
                slug = n.get("slug") or ""
                label = n["label"]
                extra = n.get("extra")
                cur = c.execute(
                    "INSERT INTO nodes(slug,label,norm_label,file_type,source_file,"
                    "source_location,definition_file,definition_location,kind,origin,"
                    "rationale,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        slug,
                        label,
                        _norm_label(label),
                        n.get("file_type", "code"),
                        source_file,
                        n.get("source_location"),
                        n.get("definition_file"),
                        n.get("definition_location"),
                        n.get("kind"),
                        n.get("origin", origin),
                        n.get("rationale"),
                        json.dumps(extra, sort_keys=True) if extra else None,
                    ),
                )
                nid = int(cur.lastrowid)
                if slug and slug not in slug_to_id:
                    slug_to_id[slug] = nid
                self._index_node(c, nid, slug, label, source_file, n.get("rationale"))

            def resolve(slug: str) -> int | None:
                if slug in slug_to_id:
                    return slug_to_id[slug]
                row = c.execute(
                    "SELECT id FROM nodes WHERE slug=? ORDER BY id LIMIT 1", (slug,)
                ).fetchone()
                return int(row["id"]) if row else None

            n_edges = 0
            for e in edges:
                s = resolve(e["src_slug"])
                d = resolve(e["dst_slug"])
                if s is None or d is None or s == d and e["relation"] in _NO_SELF:
                    continue
                _conf = e.get("confidence", "INFERRED")
                _score = e.get("confidence_score", _CONF_DEFAULT.get(_conf, 0.55))
                if _conf == "INFERRED":
                    _score = quantize_confidence(_score)
                cur = c.execute(
                    "INSERT OR IGNORE INTO edges(src,dst,relation,confidence,"
                    "confidence_score,context,source_file,source_location,weight,"
                    "evidence,deferred,type_only) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        s, d, e["relation"],
                        _conf,
                        _score,
                        e.get("context"),
                        e.get("source_file", source_file),
                        e.get("source_location"),
                        e.get("weight", 1.0),
                        e.get("evidence"),
                        1 if e.get("deferred") else 0,
                        1 if e.get("type_only") else 0,
                    ),
                )
                if cur.rowcount > 0:
                    n_edges += 1

            c.execute("DELETE FROM raw_refs WHERE source_file=?", (source_file,))
            for rc in raw_calls:
                caller = resolve(rc["caller_slug"])
                if caller is None:
                    continue
                c.execute(
                    "INSERT INTO raw_refs(caller_id,callee,relation,source_file,"
                    "source_location,import_evidence,lang,is_method,recv,recv_kind,"
                    "recv_type,caller_class,import_module,import_symbol) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        caller, rc["callee"], rc.get("relation", "calls"), source_file,
                        rc.get("source_location"),
                        1 if rc.get("import_evidence") else 0,
                        rc.get("lang"),
                        1 if rc.get("is_method") else 0,
                        rc.get("recv"), rc.get("recv_kind"), rc.get("recv_type"),
                        rc.get("caller_class"),
                        rc.get("import_module"), rc.get("import_symbol"),
                    ),
                )
            return {"nodes": len(nodes), "edges": n_edges}

    def resolve_calls(self) -> int:
        """Global cross-file resolution from ``raw_refs``.

        Idempotent: drops every previously resolved cross-file edge (``evidence``
        in ``xfile-import`` / ``xfile-infer`` / ``xfile-recv``) and rebuilds.
        High precision, deliberately low recall:

        * A callee name with more than one definition of the right kind is left
          unresolved (the single-definition god-node guard).
        * A **method call** ``x.foo()`` only resolves when the receiver's class
          is known — ``self``/``this`` (the caller's own class), a constructor
          (``Foo().bar()``), a local ``x = Foo()`` binding, or ``Foo.bar()`` —
          and the single candidate is a method of *that* class. An unknown
          receiver on a name that is called as a method from several files
          (``get``, ``execute``, ``run`` …) is never auto-linked — that is what
          used to manufacture 190-edge "god nodes".

        * The import table pins the callee to a module. A call resolves to the
          **exact file that import names** (``xfile-import`` EXTRACTED), even
          when two files define the name; a call into a module codegraph does
          not index (``op.execute``, ``os.path.join``) is dropped, not attached
          to a local namesake.

        Confidence: an import-pinned or name-imported call -> EXTRACTED 1.0; a
        unique in-package method via a typed receiver -> EXTRACTED 1.0; a weaker
        receiver-class match -> INFERRED 0.85; a bare inferred call -> the
        discrete INFERRED ladder; ``inherits`` / ``implements`` / ``imports`` ->
        EXTRACTED 1.0.
        """
        _KINDS = {
            "calls": ("function", "method", "class"),
            "inherits": ("class",),
            "implements": ("class",),
            "imports": ("file", "module"),
            "references": ("class", "function", "method"),
        }
        with self.tx() as c:
            c.execute(
                "DELETE FROM edges WHERE evidence IN "
                "('xfile-import','xfile-infer','xfile-recv')"
            )
            # (kind, name) -> [(id, source_file, class_name_or_None)]
            index: dict[tuple[str, str], list[tuple[int, str, str | None]]] = {}
            for row in c.execute(
                "SELECT id, norm_label, kind, source_file, extra FROM nodes"
            ).fetchall():
                key = (row["kind"] or "", (row["norm_label"] or "").strip(".()"))
                if not key[1]:
                    continue
                cls = None
                if row["extra"]:
                    try:
                        cls = (json.loads(row["extra"]) or {}).get("cls")
                    except (ValueError, TypeError):
                        cls = None
                index.setdefault(key, []).append(
                    (int(row["id"]), row["source_file"], _norm_label(cls) if cls else None)
                )

            all_source_files = {
                r["source_file"] for r in c.execute(
                    "SELECT DISTINCT source_file FROM nodes WHERE source_file IS NOT NULL"
                ).fetchall()
            }

            # per-file import set (for module-qualified receivers)
            imports_by_file: dict[str, set[str]] = {}
            for rc in c.execute(
                "SELECT source_file, callee FROM raw_refs WHERE relation='imports'"
            ).fetchall():
                imports_by_file.setdefault(rc["source_file"], set()).add(
                    _norm_label(rc["callee"])
                )

            # fan-in: method-call names referenced from many files with no class
            # anchor are "popular" and never auto-resolved.
            fanin: dict[str, set[str]] = {}
            for rc in c.execute(
                "SELECT callee, source_file FROM raw_refs "
                "WHERE relation='calls' AND is_method=1 "
                "AND (recv_type IS NULL OR recv_type='')"
            ).fetchall():
                fanin.setdefault(
                    _norm_label(rc["callee"]).strip(".()"), set()
                ).add(rc["source_file"])
            popular = {k for k, v in fanin.items() if len(v) >= _FANIN_LIMIT}
            popular |= _ALWAYS_POPULAR

            n = 0
            for rc in c.execute("SELECT * FROM raw_refs").fetchall():
                rel = rc["relation"] or "calls"
                if rel == "imports":
                    continue
                name = _norm_label(rc["callee"]).strip(".()")
                if not name:
                    continue
                # an aliased import (`from m import c as d`) is called as `d` but
                # the definition is named `c` — look candidates up by the real name
                lookup = name
                if rc["import_symbol"]:
                    lookup = _norm_label(rc["import_symbol"]).strip(".()") or name
                raw_cands: list[tuple[int, str, str | None]] = []
                for k in _KINDS.get(rel, ("function", "method", "class")):
                    raw_cands += index.get((k, lookup), [])
                total_before = len({nid for nid, _, _ in raw_cands})
                cands = [t for t in raw_cands if t[1] != rc["source_file"]]
                cands = list({t[0]: t for t in cands}.values())
                if not cands:
                    continue

                # import table says exactly which module the callee comes from
                mod_match = False
                if rc["import_module"]:
                    want = rc["import_module"].strip("/").lower()
                    narrowed = [t for t in cands if _stem_matches(t[1], want)]
                    if len(narrowed) == 1:
                        cands, mod_match = narrowed, True
                    elif len(narrowed) > 1:
                        cands = narrowed
                    elif not any(_stem_matches(f, want) for f in all_source_files):
                        # the callee is imported from a module we don't index
                        # (`op.execute`, `os.path.join`, `re.compile`) — external,
                        # never link it to a same-named local definition
                        continue
                    # else: named module is indexed but lacks the symbol -> a
                    # re-export; fall through to the normal guards

                recv_match = False
                if rel == "calls" and rc["is_method"]:
                    rtype = _norm_label(rc["recv_type"]) if rc["recv_type"] else None
                    if rtype:
                        by_cls = [t for t in cands if t[2] == rtype]
                        if not by_cls:
                            continue
                        cands, recv_match = by_cls, True
                    elif name in popular and not (mod_match or rc["import_evidence"]):
                        # unknown receiver on a common method name (`x.get()`,
                        # `req.headers.get()`, `conn.execute()`) — never a guess
                        continue
                    # a rare unknown-receiver name falls through to the
                    # single-candidate guard below at low confidence
                elif rel == "calls" and not rc["is_method"]:
                    if name in popular and not (mod_match or rc["import_evidence"]):
                        continue

                if len(cands) != 1:
                    continue
                dst, dst_file, _ = cands[0]
                if dst == rc["caller_id"]:
                    continue

                if rel == "calls":
                    same_root = (rc["source_file"].split("/")[0]
                                 == (dst_file or "").split("/")[0])
                    if mod_match:
                        # the import statement names this exact file — proven
                        conf, score, ev = "EXTRACTED", 1.0, "xfile-import"
                    elif rc["import_evidence"]:
                        conf, score, ev = "EXTRACTED", 1.0, "xfile-import"
                    elif recv_match and total_before == 1 and same_root:
                        # a uniquely-named method of a known in-package class,
                        # reached through a typed/constructed receiver — as solid
                        # as an import call
                        conf, score, ev = "EXTRACTED", 1.0, "xfile-recv"
                    elif recv_match:
                        conf, ev = "INFERRED", "xfile-recv"
                        score = 0.85
                    elif rc["is_method"]:
                        # method call, receiver class unknown — a guess, say so
                        conf, ev, score = "INFERRED", "xfile-infer", 0.65
                    else:
                        conf, ev = "INFERRED", "xfile-infer"
                        score = _infer_score(
                            unique_globally=(total_before == 1),
                            same_package=same_root,
                        )
                    ctx = "call"
                else:
                    conf, score, ev = "EXTRACTED", 1.0, "xfile-import"
                    ctx = "import" if rel == "imports" else "type"
                cur = c.execute(
                    "INSERT OR IGNORE INTO edges(src,dst,relation,confidence,"
                    "confidence_score,context,source_file,source_location,weight,evidence)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (rc["caller_id"], dst, rel, conf, score, ctx,
                     rc["source_file"], rc["source_location"], 1.0, ev),
                )
                if cur.rowcount > 0:
                    n += 1
            return n

    def _index_node(
        self, c: sqlite3.Connection, nid: int, slug: str, label: str,
        source_file: str, rationale: str | None,
    ) -> None:
        if self.has_fts:
            c.execute(
                "INSERT INTO nodes_fts(rowid,label,slug,source_file,rationale) "
                "VALUES(?,?,?,?,?)",
                (nid, label, slug, source_file, rationale or ""),
            )
        grams = _trigrams(label) | _trigrams(slug)
        c.executemany(
            "INSERT INTO node_trigrams(trigram,node_id) VALUES(?,?)",
            [(g, nid) for g in grams],
        )

    def reindex_fts(self) -> None:
        """Rebuild the contentless FTS + trigram tables from ``nodes``. Use after
        bulk operations that bypassed :meth:`replace_file`."""
        with self.tx() as c:
            if self.has_fts:
                c.execute("INSERT INTO nodes_fts(nodes_fts) VALUES('delete-all')")
            c.execute("DELETE FROM node_trigrams")
            for row in c.execute(
                "SELECT id,slug,label,source_file,rationale FROM nodes"
            ).fetchall():
                self._index_node(
                    c, int(row["id"]), row["slug"] or "", row["label"],
                    row["source_file"] or "", row["rationale"],
                )

    def recompute_degrees(self) -> None:
        with self.tx() as c:
            c.execute("UPDATE nodes SET degree=0")
            c.execute(
                "UPDATE nodes SET degree=("
                " SELECT COUNT(*) FROM edges e WHERE e.src=nodes.id OR e.dst=nodes.id)"
            )

    # -- reads used by render / query / analyze ---------------------------

    def nodes(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM nodes ORDER BY id").fetchall()

    def edges(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM edges ORDER BY id").fetchall()

    def stats(self) -> dict[str, Any]:
        c = self.conn
        n = c.execute("SELECT COUNT(*) n FROM nodes").fetchone()["n"]
        e = c.execute("SELECT COUNT(*) n FROM edges").fetchone()["n"]
        comm = c.execute(
            "SELECT COUNT(DISTINCT community) n FROM nodes WHERE community IS NOT NULL"
        ).fetchone()["n"]
        conf = {
            r["confidence"]: r["n"]
            for r in c.execute(
                "SELECT confidence, COUNT(*) n FROM edges GROUP BY confidence"
            ).fetchall()
        }
        files = c.execute(
            "SELECT COUNT(*) n FROM files WHERE status='present'"
        ).fetchone()["n"]
        return {"nodes": n, "edges": e, "communities": comm, "confidence": conf,
                "files": files}

    def log_query(self, **row: Any) -> None:
        cols = ("ts", "kind", "question", "corpus", "nodes_returned",
                "duration_ms", "outcome", "correction", "source_nodes")
        self.conn.execute(
            f"INSERT INTO queries({','.join(cols)}) VALUES({','.join('?' for _ in cols)})",
            [row.get(c) for c in cols],
        )
        self.conn.commit()


_NO_SELF = {"imports", "imports_from", "re_exports"}
_CONF_DEFAULT = {"EXTRACTED": 1.0, "INFERRED": 0.55, "AMBIGUOUS": 0.2}

# A method-call name referenced (with an unknown receiver) from at least this
# many distinct files is treated as too common to auto-resolve to its lone
# definition. Tuned low: precision over recall.
_FANIN_LIMIT = 4

# Method names that are common across ecosystems — never auto-link `x.<name>()`
# to a sole same-named definition without receiver-type or import evidence.
_ALWAYS_POPULAR = {
    "get", "set", "run", "execute", "call", "apply", "close", "open", "send",
    "recv", "read", "write", "flush", "load", "save", "dump", "dumps", "loads",
    "parse", "format", "build", "make", "create", "update", "delete", "remove",
    "add", "insert", "append", "extend", "pop", "clear", "copy", "start", "stop",
    "check", "validate", "init", "setup", "reset", "next", "value", "values",
    "keys", "items", "name", "id", "handle", "process", "resolve", "connect",
    "commit", "rollback", "query", "fetch", "fetchone", "fetchall", "exists",
    "to_dict", "from_dict", "json", "text", "encode", "decode", "hash",
}

# graphify's extraction spec: INFERRED confidence is one of a discrete ladder, not
# a continuous value (models otherwise collapse the range to bimodal 0.5/0.85).
_INFER_LADDER = (0.55, 0.65, 0.75, 0.85, 0.95)


def quantize_confidence(score: float) -> float:
    """Snap an INFERRED score to the nearest rung of the discrete ladder."""
    return min(_INFER_LADDER, key=lambda r: abs(r - score))


def _infer_score(*, unique_globally: bool, same_package: bool) -> float:
    if unique_globally and same_package:
        return 0.95
    if unique_globally:
        return 0.85
    if same_package:
        return 0.75
    return 0.65


def _norm_label(label: str) -> str:
    import unicodedata

    return unicodedata.normalize("NFKD", label or "").encode("ascii", "ignore").decode().lower()


_SRC_SUFFIXES = (
    ".pyi", ".py", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".go", ".rb",
    ".rs", ".java", ".kt", ".php", ".scala", ".cs", ".swift", ".c", ".cc",
    ".cpp", ".h", ".hpp", ".lua",
)


def _stem_matches(source_file: str | None, want: str) -> bool:
    """True if ``source_file`` is the module ``want`` (a posix path fragment).

    ``.../packages/seo_core/validation.py`` matches ``seo_core/validation`` and
    ``.../seo_core/pkg/__init__.py`` matches ``seo_core/pkg``.
    """
    if not source_file or not want:
        return False
    s = source_file.lower()
    for suf in _SRC_SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
            break
    if s.endswith("/__init__") or s.endswith("/index") or s.endswith("/mod"):
        s = s.rsplit("/", 1)[0]
    return s == want or s.endswith("/" + want)
