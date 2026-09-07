"""Community detection.

Louvain (networkx, seed=42) with a canonicalized edge order so the result does
not depend on ``PYTHONHASHSEED`` (graphify measured 70 vs 69 communities across
seeds without this). Leiden via ``graspologic`` is used when installed. Hubs
above the 99th degree percentile are held out and reattached by majority-vote
neighbour community; isolates get their own community; final ids are assigned
size-descending with a sorted-member tiebreak so identical groupings produce
identical ids run to run.
"""

from __future__ import annotations

import hashlib
import re

import networkx as nx

from .db import Db


def _build_graph(db: Db) -> nx.Graph:
    g = nx.Graph()
    for row in db.nodes():
        g.add_node(int(row["id"]))
    edges = [
        (int(e["src"]), int(e["dst"]), e["relation"])
        for e in db.edges()
    ]
    for u, v, rel in sorted(edges, key=lambda t: (min(t[0], t[1]), max(t[0], t[1]), t[2])):
        if u != v:
            g.add_edge(u, v)
    return g


def _partition(g: nx.Graph) -> list[set[int]]:
    try:  # pragma: no cover - optional dependency
        from graspologic.partition import leiden

        from ._util import suppressed_fds

        with suppressed_fds():  # Rust Leiden prints ANSI -> corrupts PS 5.1 scroll (#19)
            mapping = leiden(g, random_seed=42, trials=1)
        return [set(c) for c in _as_sets(mapping)]
    except Exception:
        pass
    return [set(c) for c in nx.community.louvain_communities(g, seed=42, resolution=1.0)]


def _as_sets(node_to_comm) -> list[list[int]]:
    """``leiden`` returns ``{node: community_id}``; group nodes by community."""
    pairs = node_to_comm.items() if hasattr(node_to_comm, "items") else node_to_comm
    buckets: dict[int, list[int]] = {}
    for node, comm in pairs:
        buckets.setdefault(comm, []).append(node)
    return list(buckets.values())


def cluster(db: Db) -> int:
    g = _build_graph(db)
    if g.number_of_nodes() == 0:
        return 0

    deg = dict(g.degree())
    isolates = [n for n, d in deg.items() if d == 0]
    core = g.subgraph([n for n, d in deg.items() if d > 0]).copy()

    hub_threshold = _p99(list(deg.values()))
    hubs = {n for n, d in deg.items() if d >= hub_threshold and d > 0}
    partition_graph = core.subgraph([n for n in core if n not in hubs]).copy()

    comms: list[set[int]] = _partition(partition_graph) if partition_graph.number_of_nodes() else []

    node_comm: dict[int, int] = {}
    for i, c in enumerate(comms):
        for n in c:
            node_comm[n] = i

    # reattach hubs by majority vote
    next_id = len(comms)
    for h in hubs:
        votes: dict[int, int] = {}
        for nb in g.neighbors(h):
            if nb in node_comm:
                votes[node_comm[nb]] = votes.get(node_comm[nb], 0) + 1
        if votes:
            node_comm[h] = min(votes, key=lambda k: (-votes[k], k))
        else:
            node_comm[h] = next_id
            next_id += 1
    for n in isolates:
        node_comm[n] = next_id
        next_id += 1

    # regroup and reindex size-desc with sorted-member tiebreak
    groups: dict[int, list[int]] = {}
    for n, c in node_comm.items():
        groups.setdefault(c, []).append(n)
    ordered = sorted(groups.values(), key=lambda ns: (-len(ns), tuple(sorted(ns))))
    final: dict[int, int] = {}
    for new_id, ns in enumerate(ordered):
        for n in ns:
            final[n] = new_id

    labels = _hub_labels(db, ordered)
    cohesions = {i: _cohesion(g, ns) for i, ns in enumerate(ordered)}

    with db.tx() as c:
        c.execute("DELETE FROM communities")
        for i, ns in enumerate(ordered):
            sig = hashlib.sha256(
                ",".join(map(str, sorted(ns))).encode()
            ).hexdigest()[:16]
            c.execute(
                "INSERT INTO communities(id,label,cohesion,member_sig) VALUES(?,?,?,?)",
                (i, labels[i], cohesions[i], sig),
            )
        for n, cid in final.items():
            c.execute(
                "UPDATE nodes SET community=?, community_name=? WHERE id=?",
                (cid, labels[cid], n),
            )
    return len(ordered)


def _p99(values: list[int]) -> int:
    if not values:
        return 50
    s = sorted(values)
    p = s[min(len(s) - 1, int(len(s) * 0.99))]
    return max(50, p)


def _cohesion(g: nx.Graph, nodes: list[int]) -> float:
    n = len(nodes)
    if n <= 1:
        return 1.0
    sub = g.subgraph(nodes)
    return sub.number_of_edges() / (n * (n - 1) / 2)


_STOPDIRS = {"src", "app", "lib", "pkg", "internal", "backend", "frontend",
             "server", "client", "core", "common", "shared", "packages", "apps"}


def _titleize(seg: str) -> str:
    seg = re.sub(r"[-_]+", " ", seg).strip()
    return seg[:1].upper() + seg[1:] if seg else seg


def _hub_labels(db: Db, groups: list[list[int]]) -> dict[int, str]:
    rows = {int(r["id"]): r for r in db.nodes()}
    out: dict[int, str] = {}
    for i, ns in enumerate(groups):
        members = [rows[n] for n in ns if n in rows]
        out[i] = _name_community(i, members)
    return out


_GENERIC = {"main", "run", "get", "set", "handler", "handle", "init", "setup",
            "step", "start", "stop", "send", "load", "save", "create", "update",
            "process", "execute", "call", "make", "build", "compute", "check"}


def _name_community(i: int, members: list) -> str:
    """A short descriptive label. graphify uses an LLM for this; without one we
    combine the shared directory with the 1-2 most connected real symbols, which
    is usually enough to navigate by (``Signals: score, analyse``)."""
    if not members:
        return f"Community {i}"

    code = [m for m in members if m["kind"] not in ("file", "module", "stub", "section")
            and (m["file_type"] or "code") == "code"]
    sections = [m for m in members if m["kind"] == "section"]

    # documentation cluster -> the doc's earliest / top heading
    if sections and not code:
        sections.sort(key=lambda m: ((m["source_location"] or "L999999")
                                     .lstrip("L").split("-")[0].zfill(7),
                                     -(m["degree"] or 0)))
        return sections[0]["label"]
    if not code:
        return f"Community {i}"

    files = [m["source_file"] or "" for m in code]
    dir_label = _dominant_dir(files)

    code.sort(key=lambda m: (-(m["degree"] or 0), m["label"]))
    syms: list[str] = []
    for m in code:
        s = (m["label"] or "").rstrip("()").lstrip(".")
        if s and s.lower() not in _GENERIC and s not in syms:
            syms.append(s)
        if len(syms) == 2:
            break
    syms = syms or [(code[0]["label"] or "").rstrip("()").lstrip(".")]

    sym_part = ", ".join(syms)
    if dir_label:
        return f"{dir_label}: {sym_part}" if sym_part else dir_label
    return sym_part or f"Community {i}"


def _dominant_dir(files: list[str]) -> str:
    """The most specific meaningful directory shared by most members, titleized."""
    from collections import Counter

    segs: Counter = Counter()
    for f in files:
        parts = [p for p in f.split("/")[:-1] if p]
        for depth, p in enumerate(parts):
            if p.lower() in _STOPDIRS or p.startswith((".", "(")):
                continue
            segs[p] += 1 + depth  # deeper = more specific
    if not segs:
        return ""
    best, count = segs.most_common(1)[0]
    if count < max(2, len(files) * 0.4):
        return ""
    return _titleize(best)
