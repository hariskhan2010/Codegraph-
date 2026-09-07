"""SCIP ingestion tier — scope-aware edges from a real indexer.

The tree-sitter tier resolves a cross-file call only when the callee name has a
single definition of the right kind (graphify's god-node guard: high precision,
deliberately low recall). When a `SCIP <https://github.com/sourcegraph/scip>`_
index is present (``scip-python``, ``scip-typescript``, ``rust-analyzer``,
``scip-java`` …) codegraph ingests its occurrences as ``EXTRACTED`` edges tagged
``evidence='scip'`` — precise even for overloaded / shadowed names.

Consumed via ``scip print --json`` so there is no protobuf dependency. Detection
is automatic (``*.scip`` in the tree + ``scip`` on PATH); ``extract --scip PATH``
forces a specific index, ``--no-scip`` disables it.
"""

from __future__ import annotations

import bisect
import json
import shutil
import subprocess
from pathlib import Path

from .config import nfc
from .db import Db

_ROLE_DEFINITION = 0x1
_ROLE_IMPORT = 0x2


def have_scip_cli() -> bool:
    return shutil.which("scip") is not None


def find_index(root: Path) -> Path | None:
    for name in ("index.scip", "dump.scip"):
        if (root / name).is_file():
            return root / name
    hits = sorted(root.rglob("*.scip"))
    return hits[0] if hits else None


def _load_index(scip_path: Path) -> dict | None:
    """``scip print --json`` -> dict. Accepts a pre-converted ``.json`` too."""
    if scip_path.suffix == ".json":
        try:
            return json.loads(scip_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    if not have_scip_cli():
        return None
    try:
        out = subprocess.run(
            ["scip", "print", "--json", str(scip_path)],
            capture_output=True, text=True, timeout=120, encoding="utf-8",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        # some scip versions emit newline-delimited JSON documents
        docs = [json.loads(l) for l in out.stdout.splitlines() if l.strip()]
        return {"documents": docs} if docs else None


def _occ_line(occ: dict) -> int:
    r = occ.get("range") or []
    return (r[0] + 1) if r else 0


def _is_def(occ: dict) -> bool:
    return bool(int(occ.get("symbol_roles", 0)) & _ROLE_DEFINITION)


def _kind_is_callable(kind: str | None) -> bool:
    return kind in ("function", "method")


class _FileIndex:
    """Innermost-enclosing-node lookup for a single source file."""

    def __init__(self, spans: list[tuple[int, int, int, str]]):
        # spans: (start, end, node_id, kind), sorted by start
        self._spans = sorted(spans)
        self._starts = [s[0] for s in self._spans]

    def enclosing(self, line: int) -> tuple[int, str] | None:
        best: tuple[int, str] | None = None
        best_start = -1
        i = bisect.bisect_right(self._starts, line)
        for start, end, nid, kind in self._spans[:i]:
            if start <= line <= end and start > best_start:
                best, best_start = (nid, kind), start
        return best


def _parse_loc(loc: str | None) -> tuple[int, int]:
    if not loc:
        return 0, 0
    nums = [int(x) for x in loc.replace("L", " ").replace("-", " ").split() if x.isdigit()]
    if not nums:
        return 0, 0
    return nums[0], (nums[1] if len(nums) > 1 else nums[0])


def ingest(db: Db, index: dict, root: Path) -> dict:
    """Add ``evidence='scip'`` edges from a parsed SCIP index. Idempotent:
    drops previously-ingested SCIP edges first."""
    docs = index.get("documents") or []
    if not docs:
        return {"documents": 0, "edges": 0}

    # our nodes, indexed per file for enclosing-lookup and exact-start match
    per_file: dict[str, list[tuple[int, int, int, str]]] = {}
    by_start: dict[tuple[str, int], int] = {}
    file_by_suffix: dict[str, str] = {}
    for r in db.nodes():
        sf = r["source_file"]
        if not sf:
            continue
        a, b = _parse_loc(r["source_location"])
        per_file.setdefault(sf, []).append((a, b, int(r["id"]), r["kind"] or ""))
        by_start.setdefault((sf, a), int(r["id"]))
        file_by_suffix.setdefault(sf.split("/")[-1], sf)
    findex = {sf: _FileIndex(spans) for sf, spans in per_file.items()}

    def resolve_file(rel: str) -> str | None:
        rel = nfc(rel.replace("\\", "/"))
        if rel in per_file:
            return rel
        for sf in per_file:
            if sf.endswith("/" + rel) or rel.endswith("/" + sf):
                return sf
        return file_by_suffix.get(rel.split("/")[-1])

    # pass 1: every symbol's definition site
    sym_def: dict[str, tuple[str, int]] = {}
    doc_occs: list[tuple[str, list[dict]]] = []
    for doc in docs:
        rel = resolve_file(doc.get("relative_path") or doc.get("relativePath") or "")
        occs = doc.get("occurrences") or []
        doc_occs.append((rel or "", occs))
        if not rel:
            continue
        for occ in occs:
            if _is_def(occ) and occ.get("symbol"):
                sym_def.setdefault(occ["symbol"], (rel, _occ_line(occ)))

    # pass 2: references -> edges
    n = 0
    with db.tx() as c:
        c.execute("DELETE FROM edges WHERE evidence='scip'")
        for rel, occs in doc_occs:
            if not rel or rel not in findex:
                continue
            fi = findex[rel]
            for occ in occs:
                sym = occ.get("symbol")
                roles = int(occ.get("symbol_roles", 0))
                if not sym or roles & _ROLE_DEFINITION or sym.startswith("local "):
                    continue
                target = sym_def.get(sym)
                if not target:
                    continue
                t_file, t_line = target
                dst_id = by_start.get((t_file, t_line))
                if dst_id is None:
                    tenc = findex[t_file].enclosing(t_line) if t_file in findex else None
                    dst_id = tenc[0] if tenc else None
                if dst_id is None:
                    continue
                src = fi.enclosing(_occ_line(occ))
                if src is None or src[0] == dst_id:
                    continue
                dst_kind = next((k for a, b, i, k in per_file[t_file] if i == dst_id), "")
                rel_name = "imports" if roles & _ROLE_IMPORT else (
                    "calls" if _kind_is_callable(dst_kind) else "references")
                ctx = "import" if rel_name == "imports" else (
                    "call" if rel_name == "calls" else "reference")
                cur = c.execute(
                    "INSERT OR IGNORE INTO edges(src,dst,relation,confidence,"
                    "confidence_score,context,source_file,source_location,weight,evidence)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (src[0], dst_id, rel_name, "EXTRACTED", 1.0, ctx, rel,
                     f"L{_occ_line(occ)}", 1.0, "scip"),
                )
                if cur.rowcount > 0:
                    n += 1
        db.set_meta("resolver_tier", "scip")
    return {"documents": len(docs), "edges": n}


def run(db: Db, root: Path, *, index_path: Path | None = None) -> dict | None:
    scip_path = index_path or find_index(root)
    if not scip_path or not scip_path.exists():
        return None
    index = _load_index(scip_path)
    if not index:
        return None
    stats = ingest(db, index, root)
    stats["index"] = str(scip_path)
    return stats
