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


def _hub_labels(db: Db, groups: list[list[int]]) -> dict[int, str]:
    id_to = {
        int(r["id"]): (r["label"], r["degree"] or 0, r["kind"]) for r in db.nodes()
    }
    out: dict[int, str] = {}
    for i, ns in enumerate(groups):
        present = [(*id_to[n], n) for n in ns if n in id_to]  # (label, degree, kind, n)
        if not present:
            out[i] = f"Community {i}"
            continue
        # prefer a real symbol (class/function) over the file node
        non_file = [p for p in present if p[2] not in ("file", "module", "stub")]
        pool = non_file or present
        pool.sort(key=lambda t: (-t[1], str(t[0])))
        out[i] = pool[0][0].rstrip("()").lstrip(".") or f"Community {i}"
    return out
