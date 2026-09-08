"""Graph analysis: god nodes, surprising connections, import cycles.

Read-only over the database; results are stashed in ``meta`` as JSON for the
report renderer and the MCP server. Ported from graphify's ``analyze.py`` with
the same noise filters (file/concept/builtin nodes excluded from god-node ranking).
"""

from __future__ import annotations

import json

import networkx as nx

from .db import Db

_BUILTIN_NOISE = {
    "str", "int", "float", "bool", "list", "dict", "set", "tuple", "any", "object",
    "path", "os", "sys", "self", "none", "true", "false",
}


def _digraph(db: Db) -> nx.MultiDiGraph:
    g = nx.MultiDiGraph()
    labels = {int(r["id"]): r for r in db.nodes()}
    for nid, r in labels.items():
        g.add_node(nid, **{k: r[k] for k in r.keys()})
    for e in db.edges():
        g.add_edge(int(e["src"]), int(e["dst"]), relation=e["relation"],
                   confidence=e["confidence"])
    return g


def _is_noise(row) -> bool:
    # doc headings ("section") connect only by `contains`; they are not code
    # abstractions and must not rank as god nodes.
    if row["kind"] in ("file", "concept", "module", "stub", "section"):
        return True
    if (row["file_type"] or "") in ("document", "paper"):
        return True
    lbl = (row["norm_label"] or "").rstrip("()").lstrip(".")
    return lbl in _BUILTIN_NOISE or not lbl


def god_nodes(db: Db, top_n: int = 15) -> list[dict]:
    rows = sorted(db.nodes(), key=lambda r: -(r["degree"] or 0))
    out = []
    for r in rows:
        if _is_noise(r):
            continue
        out.append({"id": int(r["id"]), "label": r["label"],
                    "slug": r["slug"], "degree": r["degree"] or 0})
        if len(out) >= top_n:
            break
    return out


def surprising_connections(db: Db, top_n: int = 8) -> list[dict]:
    by_id = {int(r["id"]): r for r in db.nodes()}
    out = []
    for e in db.edges():
        if e["relation"] in ("contains", "method", "imports", "imports_from"):
            continue
        # a weak INFERRED cross-file call is a resolver guess, not an insight —
        # keep only strong inferences and the LLM's duck-typed AMBIGUOUS edges.
        if (e["relation"] in ("calls", "references")
                and e["confidence"] == "INFERRED"
                and (e["confidence_score"] or 0) < 0.8):
            continue
        s, d = by_id.get(int(e["src"])), by_id.get(int(e["dst"]))
        if not s or not d:
            continue
        sf, df = s["source_file"], d["source_file"]
        if not sf or not df or sf == df:
            continue
        sc, dc = s["community"], d["community"]
        score = 0
        if s["community"] != d["community"]:
            score += 1
        if sf.split("/")[0] != df.split("/")[0]:
            score += 1
        if e["confidence"] == "AMBIGUOUS":
            score += 2
        elif e["confidence"] == "INFERRED":
            score += 1
        if score >= 2:
            out.append({
                "src": s["label"], "dst": d["label"], "relation": e["relation"],
                "confidence": e["confidence"], "src_file": sf, "dst_file": df,
                "score": score,
            })
    out.sort(key=lambda x: -x["score"])
    return out[:top_n]


def import_cycles(db: Db, max_len: int = 6, top_n: int = 20) -> list[dict]:
    by_id = {int(r["id"]): r for r in db.nodes()}
    fg = nx.DiGraph()
    for e in db.edges():
        if e["relation"] not in ("imports", "imports_from", "re_exports"):
            continue
        s, d = by_id.get(int(e["src"])), by_id.get(int(e["dst"]))
        if not s or not d or not s["source_file"] or not d["source_file"]:
            continue
        if s["source_file"] != d["source_file"]:
            fg.add_edge(s["source_file"], d["source_file"])
    out = []
    try:
        for cyc in nx.simple_cycles(fg, length_bound=max_len):
            out.append({"cycle": cyc, "length": len(cyc)})
            if len(out) >= top_n:
                break
    except Exception:
        pass
    out.sort(key=lambda x: x["length"])
    return out


def suggested_questions(db: Db, gods: list[dict] | None = None) -> list[str]:
    """A "first read" of an unknown repo: questions whose answers the graph can
    already give. Seeded from god nodes, entry points, and community hubs."""
    gods = gods if gods is not None else god_nodes(db, 8)
    by_id = {int(r["id"]): r for r in db.nodes()}

    # entry points: nodes with no incoming call/method edge but outgoing calls
    has_in = {int(e["dst"]) for e in db.edges()
              if e["relation"] in ("calls", "references")}
    has_out = {int(e["src"]) for e in db.edges() if e["relation"] == "calls"}
    entries = [
        by_id[n] for n in sorted(has_out - has_in)
        if n in by_id and by_id[n]["kind"] in ("function", "method", "route")
    ][:4]

    qs: list[str] = []
    for g in gods[:4]:
        name = g["label"].rstrip("()").lstrip(".")
        qs.append(f"How does {name} work?")
    for g in gods[:2]:
        name = g["label"].rstrip("()").lstrip(".")
        qs.append(f"What breaks if I change {name}?")
    for e in entries[:3]:
        name = e["label"].rstrip("()").lstrip(".")
        qs.append(f"What does {name} do end to end?")

    # cohesive *code* communities only — doc-section clusters are naturally dense
    comm_hubs = db.conn.execute(
        "SELECT c.label FROM communities c JOIN ("
        "  SELECT community, "
        "  1.0*SUM(CASE WHEN kind='section' OR file_type IN ('document','paper') "
        "              THEN 1 ELSE 0 END)/COUNT(*) AS df "
        "  FROM nodes WHERE community IS NOT NULL GROUP BY community) d "
        "ON d.community=c.id WHERE d.df < 0.5 "
        "ORDER BY c.cohesion DESC LIMIT 3"
    ).fetchall()
    for r in comm_hubs:
        if r["label"] and not r["label"].startswith("Community "):
            qs.append(f"What is the {r['label']} module responsible for?")

    seen: set[str] = set()
    return [q for q in qs if not (q in seen or seen.add(q))][:10]


def analyze(db: Db) -> dict:
    gods = god_nodes(db)
    result = {
        "god_nodes": gods,
        "surprising": surprising_connections(db),
        "import_cycles": import_cycles(db),
        "suggested_questions": suggested_questions(db, gods),
    }
    db.set_meta("analysis", json.dumps(result))
    db.conn.commit()
    return result
