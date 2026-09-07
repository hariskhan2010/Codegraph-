"""Retrieval — the part graphify does with substring matching.

Pipeline: tokenize -> stem (FTS5 porter) + trigram/edit-distance fuzzy ->
score candidates -> pick seeds -> directed graph expansion -> rerank -> render
to a token budget. ``query "authentication"`` finds ``login`` without an LLM
pre-expansion step because FTS5 stemming + fuzzy candidates bridge the wording
gap.
"""

from __future__ import annotations

import re
import time
from collections import defaultdict

from rapidfuzz import fuzz

from .db import Db

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_STOP = {
    "the", "a", "an", "of", "to", "in", "is", "are", "how", "what", "where",
    "which", "does", "do", "and", "or", "for", "on", "with", "this", "that",
    "when", "why", "who", "it", "be", "by", "from", "as", "get", "set",
    "work", "works", "working", "worked", "handle", "handled", "using",
}
_RELATIONAL = {
    "affects", "affected", "breaks", "depends", "dependency", "dependencies",
}


def _terms(question: str) -> list[str]:
    toks = [t.lower() for t in _WORD.findall(question)]
    kept = [t for t in toks if t not in _STOP and len(t) >= 2]
    return kept or toks


def _vocab(db: Db) -> list[str]:
    v: set[str] = set()
    for r in db.nodes():
        for tok in _WORD.findall(((r["norm_label"] or "") + " " + (r["slug"] or "")).lower()):
            if len(tok) >= 3:
                v.add(tok)
    return sorted(v)


def _expand_terms(db: Db, terms: list[str]) -> list[str]:
    """Local 'constrained query expansion': for each query term, add the closest
    real identifier tokens in the graph's vocabulary (fuzzy). This is the step
    graphify outsources to the host LLM."""
    from rapidfuzz import process

    vocab = _vocab(db)
    if not vocab:
        return terms
    out = list(terms)
    for t in terms:
        if t in vocab:
            continue
        for cand, score, _ in process.extract(t, vocab, scorer=fuzz.WRatio, limit=3):
            if score >= 78 and cand not in out:
                out.append(cand)
    return out


def _trigrams(s: str) -> set[str]:
    s = s.lower()
    return {s[i:i + 3] for i in range(len(s) - 2)} if len(s) >= 3 else ({s} if s else set())


def _fts_candidates(db: Db, terms: list[str]) -> dict[int, float]:
    if not db.has_fts or not terms:
        return {}
    match = " OR ".join(f'"{t}"*' for t in terms)
    out: dict[int, float] = {}
    try:
        rows = db.conn.execute(
            "SELECT rowid, bm25(nodes_fts) AS score FROM nodes_fts "
            "WHERE nodes_fts MATCH ? ORDER BY score LIMIT 200",
            (match,),
        ).fetchall()
    except Exception:
        return {}
    for r in rows:
        # bm25 is negative-better; convert to positive-better
        out[int(r["rowid"])] = -float(r["score"])
    return out


def _fuzzy_candidates(db: Db, terms: list[str]) -> dict[int, float]:
    grams = set()
    for t in terms:
        grams |= _trigrams(t)
    if not grams:
        return {}
    placeholders = ",".join("?" for _ in grams)
    rows = db.conn.execute(
        f"SELECT node_id, COUNT(*) c FROM node_trigrams WHERE trigram IN ({placeholders}) "
        "GROUP BY node_id ORDER BY c DESC LIMIT 300",
        list(grams),
    ).fetchall()
    by_id = {int(r["id"]): r for r in db.nodes()}
    out: dict[int, float] = {}
    for r in rows:
        nid = int(r["node_id"])
        node = by_id.get(nid)
        if not node:
            continue
        hay = f"{node['label']} {node['slug'] or ''}".lower()
        best = max((fuzz.partial_ratio(t, hay) for t in terms), default=0)
        if best >= 70:
            out[nid] = best / 100.0
    return out


def _score(db: Db, terms: list[str]) -> list[tuple[int, float]]:
    by_id = {int(r["id"]): r for r in db.nodes()}
    scores: dict[int, float] = defaultdict(float)
    for nid, s in _fts_candidates(db, terms).items():
        scores[nid] += 2.0 + s
    for nid, s in _fuzzy_candidates(db, terms).items():
        scores[nid] += s
    # exact / prefix bonuses, capped so one common-word hit can't dominate
    for nid, node in by_id.items():
        lbl = (node["norm_label"] or "").rstrip("()").lstrip(".")
        slug = (node["slug"] or "")
        matched = 0
        for t in terms:
            if t == lbl:
                scores[nid] += 5.0
                matched += 1
            elif lbl.startswith(t) or t in lbl:
                scores[nid] += 1.5
                matched += 1
            elif t in slug:
                scores[nid] += 0.5
        if matched and terms:
            scores[nid] *= (0.4 + 0.6 * matched / len(terms))
        # a doc heading that matches a keyword should not outrank the real symbol
        if node["kind"] == "section" or (node["file_type"] or "") in ("document", "paper"):
            scores[nid] *= 0.5
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], by_id[kv[0]]["degree"] or 0))
    return [(nid, sc) for nid, sc in ranked if sc > 0]


def _seeds(ranked: list[tuple[int, float]], max_k: int = 8) -> list[int]:
    if not ranked:
        return []
    top = ranked[0][1]
    out: list[int] = []
    for nid, sc in ranked:
        if len(out) >= max_k:
            break
        if out and sc < top * 0.15:
            break
        out.append(nid)
    return out


def _expand(db: Db, seeds: list[int], depth: int = 2) -> tuple[set[int], list[tuple]]:
    adj: dict[int, list[tuple[int, str, int]]] = defaultdict(list)
    for e in db.edges():
        s, d = int(e["src"]), int(e["dst"])
        adj[s].append((d, e["relation"], 1))
        adj[d].append((s, e["relation"], -1))
    deg = {int(r["id"]): (r["degree"] or 0) for r in db.nodes()}
    hub = max(50, sorted(deg.values())[int(len(deg) * 0.99)] if deg else 50)
    visited = set(seeds)
    frontier = set(seeds)
    edges_seen: list[tuple] = []
    for _ in range(depth):
        nxt: set[int] = set()
        for n in frontier:
            if n not in seeds and deg.get(n, 0) >= hub:
                continue
            for m, rel, direction in adj.get(n, []):
                edges_seen.append((n, m, rel) if direction == 1 else (m, n, rel))
                if m not in visited:
                    nxt.add(m)
        visited |= nxt
        frontier = nxt
    return visited, edges_seen


def _vector_boost(db: Db, question: str, ranked: list[tuple[int, float]]
                  ) -> list[tuple[int, float]]:
    """When `codegraph embed` has populated node_vec, fold nearest-neighbour
    matches into the ranking so a true synonym (`authentication` -> `login`)
    seeds the expansion even with zero lexical overlap."""
    model = db.get_meta("embed_model")
    if not model:
        return ranked
    try:
        from .embed import nearest
        from .llm import make_embed_backend

        backend = make_embed_backend(db.get_meta("embed_backend") or "")
        if backend is None:
            return ranked
        hits = nearest(db, backend, question, k=8)
    except Exception:
        return ranked
    if not hits:
        return ranked
    scores = dict(ranked)
    best = ranked[0][1] if ranked else 1.0
    for nid, sim in hits:
        scores[nid] = scores.get(nid, 0.0) + best * sim
    return sorted(scores.items(), key=lambda kv: -kv[1])


def query(db: Db, question: str, *, depth: int = 2, budget: int = 2000) -> str:
    t0 = time.time()
    base = _terms(question)
    content_terms = [t for t in base if t not in _RELATIONAL] or base
    content_terms = _expand_terms(db, content_terms)
    ranked = _score(db, content_terms)
    ranked = _vector_boost(db, question, ranked)
    seeds = _seeds(ranked)
    if not seeds:
        db.log_query(ts=t0, kind="query", question=question,
                     corpus=db.get_meta("root"), nodes_returned=0,
                     duration_ms=(time.time() - t0) * 1000)
        return "No matching nodes found."
    visited, edges_seen = _expand(db, seeds, depth)
    text = _render(db, visited, edges_seen, seeds, budget)
    db.log_query(ts=t0, kind="query", question=question,
                 corpus=db.get_meta("root"), nodes_returned=len(visited),
                 duration_ms=(time.time() - t0) * 1000)
    return text


def _render(db: Db, visited: set[int], edges_seen: list[tuple],
            seeds: list[int], budget: int) -> str:
    by_id = {int(r["id"]): r for r in db.nodes()}
    char_budget = budget * 4
    seed_set = set(seeds)

    def line_node(nid: int) -> str:
        r = by_id[nid]
        cn = f" community={r['community_name']}" if r["community_name"] else ""
        return (f"NODE {r['label']} [src={r['source_file']} "
                f"loc={r['source_location']}{cn}]")

    ordered = list(seeds) + sorted(
        (n for n in visited if n not in seed_set),
        key=lambda n: (-(by_id[n]["degree"] or 0), str(n)),
    )
    out = [
        f"Graph: {db.get_meta('root')} ({db.stats()['nodes']} nodes) | "
        f"Start: [{', '.join(by_id[s]['label'] for s in seeds)}] | "
        f"{len(visited)} nodes found",
        "",
    ]
    for nid in ordered:
        if nid in by_id:
            out.append(line_node(nid))
    out.append("")
    seen_edges = set()
    for s, d, rel in edges_seen:
        if s not in by_id or d not in by_id or (s, d, rel) in seen_edges:
            continue
        seen_edges.add((s, d, rel))
        e = _edge_conf(db, s, d, rel)
        out.append(f"EDGE {by_id[s]['label']} --{rel}{e}--> {by_id[d]['label']}")
    text = "\n".join(out)
    if len(text) > char_budget:
        cut = text.rfind("\n", 0, char_budget)
        text = text[:cut] + "\n... (truncated to budget)"
    return text


def _edge_conf(db: Db, s: int, d: int, rel: str) -> str:
    row = db.conn.execute(
        "SELECT confidence FROM edges WHERE src=? AND dst=? AND relation=? LIMIT 1",
        (s, d, rel),
    ).fetchone()
    return f" [{row['confidence']}]" if row else ""


# --------------------------------------------------------------------------- #
# explain / path / affected
# --------------------------------------------------------------------------- #


def _resolve_node(db: Db, label: str) -> tuple[int | None, str | None]:
    q = label.strip().lower()
    rows = db.nodes()
    tiers: list[list] = [[], [], [], []]
    for r in rows:
        lbl = (r["norm_label"] or "").rstrip("()").lstrip(".")
        slug = (r["slug"] or "").lower()
        if lbl == q or slug == q:
            tiers[0].append(r)
        elif lbl.startswith(q) or slug.startswith(q):
            tiers[1].append(r)
        elif q in lbl or q in slug:
            tiers[2].append(r)
    for tier in tiers:
        if not tier:
            continue
        files = {r["source_file"] for r in tier}
        if len(tier) == 1:
            return int(tier[0]["id"]), None
        if len(files) > 1:
            names = ", ".join(sorted(files)[:5])
            return None, f"Ambiguous '{label}' across: {names}"
        return int(sorted(tier, key=lambda r: -(r["degree"] or 0))[0]["id"]), None
    return None, f"No node matching '{label}'"


def _lesson_for(db: Db, label: str) -> str | None:
    import json

    refl = json.loads(db.get_meta("reflection") or "{}")
    key = label.strip(".()").lower()
    for p in refl.get("preferred", []):
        if p["node"].strip(".()").lower() == key:
            return f"preferred source ({p['pos']}x useful)"
    for d in refl.get("dead_ends", []):
        if str(d.get("node", "")).strip(".()").lower() == key:
            return "known dead end"
    return None


def explain(db: Db, label: str) -> str:
    nid, err = _resolve_node(db, label)
    if err:
        return err
    r = db.conn.execute("SELECT * FROM nodes WHERE id=?", (nid,)).fetchone()
    by_id = {int(x["id"]): x for x in db.nodes()}
    out = [
        f"Node: {r['label']}",
        f"  slug: {r['slug']}",
        f"  source: {r['source_file']}:{r['source_location']}",
        f"  kind: {r['kind']}   community: {r['community_name']}   degree: {r['degree']}",
    ]
    if r["rationale"]:
        out.append(f"  rationale: {r['rationale']}")
    _lesson = _lesson_for(db, r["label"])
    if _lesson:
        out.append(f"  lesson: {_lesson}")
    out.append("")
    outgoing = db.conn.execute(
        "SELECT dst,relation,confidence FROM edges WHERE src=? ORDER BY relation", (nid,)
    ).fetchall()
    incoming = db.conn.execute(
        "SELECT src,relation,confidence FROM edges WHERE dst=? ORDER BY relation", (nid,)
    ).fetchall()
    for e in outgoing[:25]:
        t = by_id.get(int(e["dst"]))
        if t:
            out.append(f"  --{e['relation']} [{e['confidence']}]--> {t['label']}  ({t['source_file']})")
    for e in incoming[:25]:
        s = by_id.get(int(e["src"]))
        if s:
            out.append(f"  {s['label']} --{e['relation']} [{e['confidence']}]-->  ({s['source_file']})")
    return "\n".join(out)


def affected(db: Db, label: str, *, depth: int = 3) -> str:
    """Reverse traversal over calls / references / imports_from / implements —
    "what breaks if I change this"."""
    nid, err = _resolve_node(db, label)
    if err:
        return err
    by_id = {int(r["id"]): r for r in db.nodes()}
    rev: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for e in db.edges():
        if e["relation"] in ("calls", "references", "imports_from", "implements",
                             "inherits", "method", "contains"):
            rev[int(e["dst"])].append((int(e["src"]), e["relation"]))
    seen = {nid}
    layers: list[list[tuple[int, str, int]]] = []
    frontier = [nid]
    for hop in range(depth):
        nxt = []
        layer = []
        for n in frontier:
            for s, rel in rev.get(n, []):
                if s not in seen:
                    seen.add(s)
                    nxt.append(s)
                    layer.append((s, rel, hop + 1))
        if layer:
            layers.append(layer)
        frontier = nxt
    r = by_id[nid]
    out = [f"Affected by a change to {r['label']} ({r['source_file']}):", ""]
    if not layers:
        out.append("  (nothing depends on this symbol)")
    for layer in layers:
        for s, rel, hop in sorted(layer, key=lambda t: (t[2], by_id[t[0]]['source_file'])):
            n = by_id[s]
            out.append(f"  [{hop}] {n['label']}  <--{rel}--  ({n['source_file']}:{n['source_location']})")
    out.append("")
    out.append(f"{len(seen) - 1} symbols affected across "
               f"{len({by_id[s]['source_file'] for s in seen if s != nid})} files.")
    return "\n".join(out)


def shortest_path(db: Db, a: str, b: str, *, max_hops: int = 8) -> str:
    import networkx as nx

    sa, ea = _resolve_node(db, a)
    sb, eb = _resolve_node(db, b)
    if ea:
        return ea
    if eb:
        return eb
    g = nx.DiGraph()
    for e in db.edges():
        g.add_edge(int(e["src"]), int(e["dst"]), relation=e["relation"])
    by_id = {int(r["id"]): r for r in db.nodes()}
    try:
        p = nx.shortest_path(g, sa, sb)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        try:
            p = nx.shortest_path(g.to_undirected(), sa, sb)
        except Exception:
            return f"No path between {a} and {b}."
    if len(p) - 1 > max_hops:
        return f"Shortest path is {len(p) - 1} hops (> max {max_hops})."
    out = [f"Path ({len(p) - 1} hops):"]
    for u, v in zip(p, p[1:]):
        rel = g.get_edge_data(u, v)
        r = rel["relation"] if rel else "~"
        out.append(f"  {by_id[u]['label']}  --{r}-->  {by_id[v]['label']}")
    return "\n".join(out)
