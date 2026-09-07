"""Export targets — every one is a pure function over the SQLite store.

graphify ships ~10 exporters and 6 graph-DB integrations; codegraph keeps the
formats that are a deterministic serialization of the graph (no live database
connection, no network) and leaves the hosted-service loaders to Phase 3 plugins.

``codegraph export <target> <path>`` writes into ``graphify-out/exports/``:

  graphml  gexf     — Gephi / yEd / NetworkX
  dot                — Graphviz (``dot -Tsvg``)
  cypher             — Neo4j / FalkorDB / Memgraph import script
  csv                — nodes.csv + edges.csv (neo4j-admin, pandas)
  jsonl              — nodes.jsonl + edges.jsonl (GraphRAG pipelines)
  mermaid            — a ```mermaid``` flowchart (per-community subgraphs)
  obsidian           — a Markdown vault, one note per node, wikilinks for edges
  tree               — plain-text containment outline
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.sax.saxutils as sx
from pathlib import Path

from .config import out_dir
from .db import Db

TARGETS = ("graphml", "gexf", "dot", "cypher", "csv", "jsonl", "mermaid",
           "obsidian", "tree")


def _rows(db: Db):
    nodes = db.nodes()
    id_ok = {int(r["id"]) for r in nodes}
    edges = [e for e in db.edges()
             if int(e["src"]) in id_ok and int(e["dst"]) in id_ok]
    return nodes, edges


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", name).strip() or "node"


# --------------------------------------------------------------------------- #
# XML family
# --------------------------------------------------------------------------- #

def to_graphml(db: Db) -> str:
    nodes, edges = _rows(db)
    keys = [
        ("label", "d0", "string"), ("kind", "d1", "string"),
        ("source_file", "d2", "string"), ("source_location", "d3", "string"),
        ("community", "d4", "long"), ("community_name", "d5", "string"),
        ("degree", "d6", "long"), ("rationale", "d7", "string"),
    ]
    ekeys = [("relation", "e0", "string"), ("confidence", "e1", "string"),
             ("confidence_score", "e2", "double"), ("source_location", "e3", "string")]
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<graphml xmlns="http://graphml.graphdrawing.org/xmlns">\n')
    for attr, kid, typ in keys:
        out.write(f'  <key id="{kid}" for="node" attr.name="{attr}" attr.type="{typ}"/>\n')
    for attr, kid, typ in ekeys:
        out.write(f'  <key id="{kid}" for="edge" attr.name="{attr}" attr.type="{typ}"/>\n')
    out.write('  <graph edgedefault="directed">\n')
    for r in nodes:
        out.write(f'    <node id="n{r["id"]}">\n')
        for attr, kid, _ in keys:
            v = r[attr]
            if v is None or v == "":
                continue
            out.write(f'      <data key="{kid}">{sx.escape(str(v))}</data>\n')
        out.write("    </node>\n")
    for i, e in enumerate(edges):
        out.write(f'    <edge id="e{i}" source="n{e["src"]}" target="n{e["dst"]}">\n')
        for attr, kid, _ in ekeys:
            v = e[attr]
            if v is None or v == "":
                continue
            out.write(f'      <data key="{kid}">{sx.escape(str(v))}</data>\n')
        out.write("    </edge>\n")
    out.write("  </graph>\n</graphml>\n")
    return out.getvalue()


def to_gexf(db: Db) -> str:
    nodes, edges = _rows(db)
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write('<gexf xmlns="http://gexf.net/1.3" version="1.3">\n')
    out.write('  <graph defaultedgetype="directed">\n')
    out.write('    <attributes class="node">\n'
              '      <attribute id="0" title="kind" type="string"/>\n'
              '      <attribute id="1" title="community" type="string"/>\n'
              '      <attribute id="2" title="source_file" type="string"/>\n'
              '    </attributes>\n')
    out.write("    <nodes>\n")
    for r in nodes:
        out.write(f'      <node id="{r["id"]}" label="{sx.quoteattr(r["label"] or "")[1:-1]}">\n')
        out.write('        <attvalues>\n')
        out.write(f'          <attvalue for="0" value="{sx.escape(r["kind"] or "")}"/>\n')
        out.write(f'          <attvalue for="1" value="{sx.escape(str(r["community_name"] or r["community"] or ""))}"/>\n')
        out.write(f'          <attvalue for="2" value="{sx.escape(r["source_file"] or "")}"/>\n')
        out.write('        </attvalues>\n      </node>\n')
    out.write("    </nodes>\n    <edges>\n")
    for i, e in enumerate(edges):
        out.write(f'      <edge id="{i}" source="{e["src"]}" target="{e["dst"]}" '
                  f'label="{sx.escape(e["relation"] or "")}" weight="{e["weight"] or 1.0}"/>\n')
    out.write("    </edges>\n  </graph>\n</gexf>\n")
    return out.getvalue()


# --------------------------------------------------------------------------- #
# text family
# --------------------------------------------------------------------------- #

def to_dot(db: Db) -> str:
    nodes, edges = _rows(db)
    palette = ["#5b9dd9", "#d98b5b", "#7bd95b", "#d95b9d", "#d9cf5b",
               "#5bd9cf", "#9d5bd9", "#d95b5b"]
    out = ["digraph codegraph {", '  rankdir=LR; node [style=filled, shape=box, '
           'fontname="Helvetica", fontsize=10];']
    for r in nodes:
        c = r["community"]
        color = palette[c % len(palette)] if c is not None else "#cccccc"
        lbl = (r["label"] or "").replace('"', "'")
        out.append(f'  n{r["id"]} [label="{lbl}", fillcolor="{color}"];')
    for e in edges:
        style = "" if e["confidence"] == "EXTRACTED" else ' [style=dashed]'
        out.append(f'  n{e["src"]} -> n{e["dst"]}{style};')
    out.append("}")
    return "\n".join(out) + "\n"


def to_cypher(db: Db) -> str:
    nodes, edges = _rows(db)
    out = ["// codegraph -> Neo4j / FalkorDB / Memgraph",
           "// run:  cat graph.cypher | cypher-shell",
           "CREATE CONSTRAINT cg_id IF NOT EXISTS FOR (n:Symbol) REQUIRE n.id IS UNIQUE;"]

    def props(d: dict) -> str:
        items = []
        for k, v in d.items():
            if v is None or v == "":
                continue
            if isinstance(v, (int, float)):
                items.append(f"{k}: {v}")
            else:
                items.append(f"{k}: {json.dumps(str(v))}")
        return "{" + ", ".join(items) + "}"

    for r in nodes:
        out.append(
            f"MERGE (n:Symbol {{id: {int(r['id'])}}}) SET n += "
            + props({"label": r["label"], "slug": r["slug"], "kind": r["kind"],
                     "source_file": r["source_file"], "source_location": r["source_location"],
                     "community": r["community"], "community_name": r["community_name"],
                     "degree": r["degree"], "rationale": r["rationale"]}) + ";")
    for e in edges:
        rel = re.sub(r"[^A-Z_]", "_", (e["relation"] or "REL").upper()) or "REL"
        out.append(
            f"MATCH (a:Symbol {{id: {int(e['src'])}}}), (b:Symbol {{id: {int(e['dst'])}}}) "
            f"MERGE (a)-[r:{rel}]->(b) SET r += "
            + props({"confidence": e["confidence"], "confidence_score": e["confidence_score"],
                     "source_file": e["source_file"], "source_location": e["source_location"],
                     "weight": e["weight"], "evidence": e["evidence"]}) + ";")
    return "\n".join(out) + "\n"


def to_mermaid(db: Db, *, max_nodes: int = 120) -> str:
    nodes, edges = _rows(db)
    keep = sorted(nodes, key=lambda r: -(r["degree"] or 0))[:max_nodes]
    kept = {int(r["id"]) for r in keep}
    by_comm: dict[str, list] = {}
    for r in keep:
        by_comm.setdefault(r["community_name"] or f"community {r['community']}", []).append(r)
    out = ["```mermaid", "flowchart LR"]
    for name, members in by_comm.items():
        out.append(f'  subgraph {json.dumps(name)}')
        for r in members:
            lbl = json.dumps((r["label"] or "")[:40])
            out.append(f"    n{r['id']}[{lbl}]")
        out.append("  end")
    for e in edges:
        if int(e["src"]) in kept and int(e["dst"]) in kept:
            arrow = "-->" if e["confidence"] == "EXTRACTED" else "-.->"
            out.append(f'  n{e["src"]} {arrow}|{e["relation"]}| n{e["dst"]}')
    out.append("```")
    return "\n".join(out) + "\n"


def to_tree(db: Db) -> str:
    nodes, edges = _rows(db)
    by_id = {int(r["id"]): r for r in nodes}
    children: dict[int, list[int]] = {}
    has_parent: set[int] = set()
    for e in edges:
        if e["relation"] in ("contains", "method"):
            children.setdefault(int(e["src"]), []).append(int(e["dst"]))
            has_parent.add(int(e["dst"]))
    roots = sorted((int(r["id"]) for r in nodes if int(r["id"]) not in has_parent),
                   key=lambda n: (by_id[n]["source_file"] or "", by_id[n]["label"] or ""))
    out: list[str] = []

    def walk(nid: int, depth: int) -> None:
        r = by_id[nid]
        loc = f"  ({r['source_file']}:{r['source_location']})" if r["source_file"] else ""
        out.append("  " * depth + f"{r['label']}{loc}")
        for ch in sorted(children.get(nid, []),
                         key=lambda n: by_id[n]["source_location"] or ""):
            walk(ch, depth + 1)

    for root in roots:
        walk(root, 0)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# multi-file targets
# --------------------------------------------------------------------------- #

def _csv_pair(db: Db) -> tuple[str, str]:
    nodes, edges = _rows(db)
    nb = io.StringIO()
    w = csv.writer(nb)
    w.writerow(["id", "label", "slug", "kind", "source_file", "source_location",
                "community", "community_name", "degree", "rationale"])
    for r in nodes:
        w.writerow([r["id"], r["label"], r["slug"], r["kind"], r["source_file"],
                    r["source_location"], r["community"], r["community_name"],
                    r["degree"], r["rationale"]])
    eb = io.StringIO()
    w = csv.writer(eb)
    w.writerow(["src", "dst", "relation", "confidence", "confidence_score",
                "source_file", "source_location", "weight", "evidence"])
    for e in edges:
        w.writerow([e["src"], e["dst"], e["relation"], e["confidence"],
                    e["confidence_score"], e["source_file"], e["source_location"],
                    e["weight"], e["evidence"]])
    return nb.getvalue(), eb.getvalue()


def _jsonl_pair(db: Db) -> tuple[str, str]:
    nodes, edges = _rows(db)
    nl = "\n".join(json.dumps({
        "id": int(r["id"]), "label": r["label"], "slug": r["slug"], "kind": r["kind"],
        "source_file": r["source_file"], "source_location": r["source_location"],
        "community": r["community"], "community_name": r["community_name"],
        "degree": r["degree"], "rationale": r["rationale"],
    }, ensure_ascii=False) for r in nodes)
    el = "\n".join(json.dumps({
        "source": int(e["src"]), "target": int(e["dst"]), "relation": e["relation"],
        "confidence": e["confidence"], "confidence_score": e["confidence_score"],
        "source_file": e["source_file"], "source_location": e["source_location"],
        "weight": e["weight"], "evidence": e["evidence"],
    }, ensure_ascii=False) for e in edges)
    return nl + "\n", el + "\n"


def _obsidian_vault(db: Db, root: Path) -> int:
    nodes, edges = _rows(db)
    by_id = {int(r["id"]): r for r in nodes}
    out_edges: dict[int, list] = {}
    in_edges: dict[int, list] = {}
    for e in edges:
        out_edges.setdefault(int(e["src"]), []).append(e)
        in_edges.setdefault(int(e["dst"]), []).append(e)
    names: dict[int, str] = {}
    used: set[str] = set()
    for r in nodes:
        base = _safe(f"{r['label']} ({r['id']})")
        names[int(r["id"])] = base
        used.add(base)
    root.mkdir(parents=True, exist_ok=True)
    for r in nodes:
        nid = int(r["id"])
        L = [
            "---",
            f"kind: {r['kind']}",
            f"community: {r['community_name'] or r['community']}",
            f"source: {r['source_file']}:{r['source_location']}",
            f"degree: {r['degree']}",
            "---",
            f"# {r['label']}",
            "",
        ]
        if r["rationale"]:
            L += [r["rationale"], ""]
        if out_edges.get(nid):
            L.append("## calls / references")
            for e in out_edges[nid]:
                t = by_id.get(int(e["dst"]))
                if t:
                    L.append(f"- {e['relation']} → [[{names[int(e['dst'])]}]]  "
                             f"`[{e['confidence']}]`")
            L.append("")
        if in_edges.get(nid):
            L.append("## called by / referenced by")
            for e in in_edges[nid]:
                s = by_id.get(int(e["src"]))
                if s:
                    L.append(f"- [[{names[int(e['src'])]}]] {e['relation']} →")
            L.append("")
        (root / f"{names[nid]}.md").write_text("\n".join(L), encoding="utf-8")
    return len(nodes)


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #

def load_cypher(db: Db, *, uri: str | None = None, user: str | None = None,
                password: str | None = None) -> dict:
    """Push the graph into a live Neo4j / FalkorDB / Memgraph.

    Uses the ``neo4j`` Python driver when installed, else pipes the generated
    script through ``cypher-shell`` if it is on PATH.
    """
    import os
    import shutil
    import subprocess

    script = to_cypher(db)
    statements = [s.strip() for s in script.splitlines()
                  if s.strip() and not s.strip().startswith("//")]
    uri = uri or os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    user = user or os.environ.get("NEO4J_USER", "neo4j")
    password = password or os.environ.get("NEO4J_PASSWORD", "")

    try:
        from neo4j import GraphDatabase  # type: ignore

        with GraphDatabase.driver(uri, auth=(user, password)) as drv:
            with drv.session() as s:
                for stmt in statements:
                    s.run(stmt)
        return {"driver": "neo4j-python", "statements": len(statements), "uri": uri}
    except ImportError:
        pass

    if shutil.which("cypher-shell"):
        env = {**os.environ, "NEO4J_PASSWORD": password} if password else None
        r = subprocess.run(
            ["cypher-shell", "-a", uri, "-u", user,
             *(["-p", password] if password else [])],
            input=script, capture_output=True, text=True, env=env, timeout=300,
        )
        if r.returncode != 0:
            raise RuntimeError(f"cypher-shell failed: {r.stderr[:400]}")
        return {"driver": "cypher-shell", "statements": len(statements), "uri": uri}

    raise RuntimeError(
        "live load needs the `neo4j` Python package (pip install neo4j) or "
        "`cypher-shell` on PATH; `codegraph export cypher` writes the script for "
        "manual import")


def export(db: Db, target: str, project_root: Path) -> list[Path]:
    exp = out_dir(project_root) / "exports"
    exp.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def w(name: str, text: str) -> None:
        p = exp / name
        p.write_text(text, encoding="utf-8")
        written.append(p)

    if target == "graphml":
        w("graph.graphml", to_graphml(db))
    elif target == "gexf":
        w("graph.gexf", to_gexf(db))
    elif target == "dot":
        w("graph.dot", to_dot(db))
    elif target == "cypher":
        w("graph.cypher", to_cypher(db))
    elif target == "mermaid":
        w("graph.mmd", to_mermaid(db))
    elif target == "tree":
        w("tree.txt", to_tree(db))
    elif target == "csv":
        nb, eb = _csv_pair(db)
        w("nodes.csv", nb)
        w("edges.csv", eb)
    elif target == "jsonl":
        nl, el = _jsonl_pair(db)
        w("nodes.jsonl", nl)
        w("edges.jsonl", el)
    elif target == "obsidian":
        vault = exp / "obsidian"
        n = _obsidian_vault(db, vault)
        written.append(vault)
        return written
    else:
        raise ValueError(f"unknown export target: {target}")
    return written
