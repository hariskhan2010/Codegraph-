"""``codegraph diagnose`` — graph-health check.

graphify's ``diagnose`` ported to a set of ``SELECT``s. Reports the things that
quietly degrade a graph: parse failures, unresolved cross-file references
(retrieval recall), a runaway god-community, low structural confidence, slug
collisions, semantic-coverage gaps, and staleness vs. git HEAD.
"""

from __future__ import annotations

from pathlib import Path

from .db import Db
from .pipeline import _git_head


def diagnose(db: Db) -> dict:
    c = db.conn
    n_nodes = c.execute("SELECT COUNT(*) n FROM nodes").fetchone()["n"]
    n_edges = c.execute("SELECT COUNT(*) n FROM edges").fetchone()["n"]

    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str, hint: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "hint": hint})

    # 1. parse failures — present code files with no symbol node
    files = c.execute(
        "SELECT path FROM files WHERE file_type='code' AND status='present'"
    ).fetchall()
    with_nodes = {
        r["source_file"]
        for r in c.execute("SELECT DISTINCT source_file FROM nodes").fetchall()
    }
    empty = sorted(f["path"] for f in files if f["path"] not in with_nodes)
    add("parse_coverage", not empty,
        f"{len(files) - len(empty)}/{len(files)} code files produced nodes",
        f"no symbols from: {', '.join(empty[:8])}" if empty else "")

    # 2. unresolved cross-file refs — retrieval-recall canary
    raw = c.execute("SELECT COUNT(*) n FROM raw_refs").fetchone()["n"]
    xfile = c.execute(
        "SELECT COUNT(*) n FROM edges WHERE evidence IN ('xfile-import','xfile-infer')"
    ).fetchone()["n"]
    scip_n = c.execute("SELECT COUNT(*) n FROM edges WHERE evidence='scip'").fetchone()["n"]
    lsp_n = c.execute("SELECT COUNT(*) n FROM edges WHERE evidence='lsp'").fetchone()["n"]
    tier = db.get_meta("resolver_tier") or "tree-sitter"
    ratio = (xfile + scip_n + lsp_n) / raw if raw else 1.0
    add("xref_resolution", ratio >= 0.15 or raw < 20,
        f"{xfile} heuristic + {scip_n} scip + {lsp_n} lsp edges vs {raw} raw refs "
        f"({ratio:.0%}); tier={tier}",
        "low resolution is expected with the single-definition guard; run a SCIP "
        "indexer (`extract --scip`) or a language server (`extract --lsp`) for "
        "scope-aware recall" if ratio < 0.15 else "")

    # 3. god-community — one community swallowing the graph
    sizes = c.execute(
        "SELECT community, COUNT(*) n FROM nodes WHERE community IS NOT NULL "
        "GROUP BY community ORDER BY n DESC"
    ).fetchall()
    if sizes and n_nodes:
        top = sizes[0]["n"] / n_nodes
        add("community_balance", top <= 0.6,
            f"largest community holds {sizes[0]['n']} nodes ({top:.0%})",
            "a dominant community usually means a hub was not held out" if top > 0.6 else "")

    # 4. structural confidence
    conf = {
        r["confidence"]: r["n"]
        for r in c.execute(
            "SELECT confidence, COUNT(*) n FROM edges GROUP BY confidence"
        ).fetchall()
    }
    extracted = conf.get("EXTRACTED", 0)
    pct = extracted / n_edges if n_edges else 1.0
    add("provenance", pct >= 0.4 or n_edges < 20,
        f"{pct:.0%} of edges are EXTRACTED "
        f"({conf.get('INFERRED', 0)} INFERRED, {conf.get('AMBIGUOUS', 0)} AMBIGUOUS)")

    # 5. slug collisions
    dupes = c.execute(
        "SELECT slug, COUNT(*) n FROM nodes WHERE slug != '' GROUP BY slug HAVING n > 1"
    ).fetchall()
    add("slug_collisions", True,
        f"{len(dupes)} slugs map to >1 node "
        f"(disambiguated with #2/#3 in graph.json — not a defect)")

    # 6. dangling edges — must be zero by construction
    dangling = c.execute(
        "SELECT COUNT(*) n FROM edges e WHERE "
        "NOT EXISTS(SELECT 1 FROM nodes WHERE id=e.src) OR "
        "NOT EXISTS(SELECT 1 FROM nodes WHERE id=e.dst)"
    ).fetchone()["n"]
    add("referential_integrity", dangling == 0,
        f"{dangling} edges with a missing endpoint",
        "run `codegraph extract --force`" if dangling else "")

    # 7. semantic coverage
    sem = c.execute(
        "SELECT COUNT(*) n FROM files WHERE file_type='code' AND status='present' "
        "AND semantic_hash != '' AND semantic_hash = content_sha256"
    ).fetchone()["n"]
    total_code = len(files) or 1
    add("semantic_coverage", True,
        f"{sem}/{total_code} files annotated by an LLM ({sem / total_code:.0%})",
        "run `codegraph extract` with a backend for rationale + AMBIGUOUS edges"
        if sem < total_code else "")

    # 8. staleness
    built = db.get_meta("built_at_commit")
    root = Path(db.get_meta("root") or ".")
    head = _git_head(root)
    if built and head:
        add("freshness", built[:12] == head[:12],
            f"built at {built[:12]}, HEAD is {head[:12]}",
            "run `codegraph update`" if built[:12] != head[:12] else "")

    return {
        "nodes": n_nodes, "edges": n_edges,
        "checks": checks,
        "failed": [c["check"] for c in checks if not c["ok"]],
    }


def format_report(d: dict) -> str:
    out = [f"codegraph diagnose — {d['nodes']} nodes · {d['edges']} edges", ""]
    for c in d["checks"]:
        mark = "ok  " if c["ok"] else "WARN"
        out.append(f"  [{mark}] {c['check']}: {c['detail']}")
        if c["hint"] and not c["ok"]:
            out.append(f"         → {c['hint']}")
    out.append("")
    out.append("all checks passed" if not d["failed"]
               else f"{len(d['failed'])} warning(s): {', '.join(d['failed'])}")
    return "\n".join(out)
