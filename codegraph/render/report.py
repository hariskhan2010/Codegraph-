"""Render ``GRAPH_REPORT.md`` — the provenance-first "first read" of a repo.

Section order follows graphify's ``report.generate``: Summary, Graph Freshness,
Community Hubs, God Nodes, Surprising Connections, Import Cycles, Communities.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from ..config import REPORT_NAME
from ..db import Db


def build_report(db: Db) -> str:
    st = db.stats()
    analysis = json.loads(db.get_meta("analysis") or "{}")
    root = db.get_meta("root") or "."
    name = root.rstrip("/").rsplit("/", 1)[-1]
    L: list[str] = []
    add = L.append

    add(f"# Graph Report - {name}  ({date.today().isoformat()})")
    add("")
    conf = st["confidence"]
    total_e = st["edges"] or 1
    pct = {k: round(100 * conf.get(k, 0) / total_e) for k in ("EXTRACTED", "INFERRED", "AMBIGUOUS")}
    add("## Summary")
    add("")
    add(f"- {st['nodes']} nodes · {st['edges']} edges · {st['communities']} communities "
        f"· {st['files']} files")
    add(f"- Extraction: {pct['EXTRACTED']}% EXTRACTED · {pct['INFERRED']}% INFERRED "
        f"· {pct['AMBIGUOUS']}% AMBIGUOUS")
    add("")

    head = db.get_meta("built_at_commit")
    if head:
        add("## Graph Freshness")
        add("")
        add(f"- Built from commit: `{head[:12]}`")
        add("- Re-run `codegraph update .` after pulling changes.")
        add("")

    comm_rows = db.conn.execute(
        "SELECT id,label,cohesion FROM communities ORDER BY id"
    ).fetchall()
    sizes = {
        r["community"]: r["n"]
        for r in db.conn.execute(
            "SELECT community, COUNT(*) n FROM nodes WHERE community IS NOT NULL "
            "GROUP BY community"
        ).fetchall()
    }
    # classify each community as code vs docs so markdown-heavy repos don't drown
    # the code structure in the navigation list
    doc_frac = {
        r["community"]: (r["docs"] / r["total"] if r["total"] else 0.0)
        for r in db.conn.execute(
            "SELECT community, COUNT(*) total, "
            "SUM(CASE WHEN kind IN ('section','concept') "
            "         OR file_type IN ('document','paper','concept','transcript','image') "
            "         THEN 1 ELSE 0 END) docs "
            "FROM nodes WHERE community IS NOT NULL GROUP BY community"
        ).fetchall()
    }
    code_comms = [r for r in comm_rows if doc_frac.get(r["id"], 0) < 0.5]
    doc_comms = [r for r in comm_rows if doc_frac.get(r["id"], 0) >= 0.5]

    if code_comms:
        add("## Community Hubs (Navigation)")
        add("")
        for r in code_comms:
            if sizes.get(r["id"], 0) >= 3:
                add(f"- **{r['label']}** — {sizes.get(r['id'], 0)} nodes")
        add("")
        if doc_comms:
            add(f"_{len(doc_comms)} documentation-only communities omitted here "
                "(see Communities below)._")
            add("")

    gods = analysis.get("god_nodes", [])
    if gods:
        add("## God Nodes (most connected — your core abstractions)")
        add("")
        for i, g in enumerate(gods, 1):
            add(f"{i}. `{g['label']}` — {g['degree']} edges")
        add("")

    surprises = analysis.get("surprising", [])
    add("## Surprising Connections (you probably didn't know these)")
    add("")
    if surprises:
        for s in surprises:
            add(f"- `{s['src']}` --{s['relation']}--> `{s['dst']}`  [{s['confidence']}]")
            add(f"  {s['src_file']} → {s['dst_file']}")
    else:
        add("- None detected — all connections are within the same source files.")
    add("")

    questions = analysis.get("suggested_questions", [])
    if questions:
        add("## Suggested Questions (the graph can already answer these)")
        add("")
        for q in questions:
            add(f"- `codegraph query \"{q}\"`")
        add("")

    cycles = analysis.get("import_cycles", [])
    if cycles:
        add("## Import Cycles")
        add("")
        for c in cycles:
            add(f"- {c['length']}-file cycle: `{' -> '.join(c['cycle'] + c['cycle'][:1])}`")
        add("")

    refl = json.loads(db.get_meta("reflection") or "{}")
    if refl.get("preferred") or refl.get("dead_ends"):
        add("## Work-memory lessons")
        add("")
        if refl.get("preferred"):
            add("**Preferred sources** — corroborated by repeated useful results; start here.")
            for p in refl["preferred"]:
                add(f"- `{p['node']}` ({p['pos']}x useful)")
            add("")
        if refl.get("dead_ends"):
            add("**Known dead ends** — led nowhere; don't re-derive.")
            for d in refl["dead_ends"]:
                add(f"- {d.get('node') or d.get('question')}")
            add("")

    if comm_rows:
        add(f"## Communities ({len(code_comms)} code, {len(doc_comms)} docs)")
        add("")

        def _emit(rows, heading):
            if not rows:
                return
            add(f"### {heading}")
            add("")
            for r in rows:
                n = sizes.get(r["id"], 0)
                if n < 3:
                    continue
                members = [
                    row["label"]
                    for row in db.conn.execute(
                        "SELECT label FROM nodes WHERE community=? "
                        "ORDER BY degree DESC LIMIT 8",
                        (r["id"],),
                    ).fetchall()
                ]
                add(f'#### Community {r["id"]} — "{r["label"]}"  '
                    f"(cohesion {r['cohesion']:.2f})")
                add(f"{', '.join(members)}" + (" (+more)" if n > 8 else ""))
                add("")

        _emit(code_comms, "Code")
        _emit(doc_comms, "Documentation")

    return "\n".join(L).rstrip() + "\n"


def write_report(db: Db, out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    target = out / REPORT_NAME
    target.write_text(build_report(db), encoding="utf-8")
    return target
