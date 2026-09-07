"""Render ``graph.json`` from the database — graphify's on-disk contract.

* top level: ``directed``, ``multigraph``, ``graph``, ``nodes``, ``links``
  (key is ``links``), ``hyperedges``, ``built_at_commit``
* node: ``id`` (= slug), ``label`` first, then keys sorted; always ``community``
  (int|null) and ``norm_label``; ``community_name`` only when the community has a
  label
* link: ``source``, ``target``, ``relation`` first, then sorted; ``source`` /
  ``target`` carry true caller->callee direction; ``confidence_score`` always
  present (filled from the confidence enum if missing)
* determinism: identity keys first, remaining ``sorted()``; ``nodes`` and
  ``links`` sorted by canonical compact JSON; atomic write, ``indent=2``

Slug collisions (allowed in the DB, where the integer PK is identity) are
disambiguated here with ``#2`` / ``#3`` suffixes so two real symbols never share
one ``graph.json`` id.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .._util import atomic_replace
from ..config import GRAPH_JSON_NAME
from ..db import Db

_CONF_SCORE = {"EXTRACTED": 1.0, "INFERRED": 0.55, "AMBIGUOUS": 0.2}


def _canonical(d: dict, first: tuple[str, ...]) -> dict:
    out: dict = {}
    for k in first:
        if k in d:
            out[k] = d[k]
    for k in sorted(d):
        if k not in first:
            out[k] = d[k]
    return out


def _sort_key(item: dict) -> str:
    return json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def build_graph_json(db: Db) -> dict:
    rows = db.nodes()
    id_to_slug: dict[int, str] = {}
    used: dict[str, int] = {}
    for r in rows:
        base = r["slug"] or f"node_{r['id']}"
        used[base] = used.get(base, 0) + 1
        slug = base if used[base] == 1 else f"{base}#{used[base]}"
        id_to_slug[int(r["id"])] = slug

    nodes_out = []
    for r in rows:
        extra = json.loads(r["extra"]) if r["extra"] else {}
        n = {
            "id": id_to_slug[int(r["id"])],
            "label": r["label"],
            "file_type": r["file_type"],
            "source_file": r["source_file"],
            "source_location": r["source_location"],
            "kind": r["kind"],
            "norm_label": r["norm_label"],
            "community": r["community"],
        }
        if r["community_name"] is not None and r["community"] is not None:
            n["community_name"] = r["community_name"]
        if r["rationale"]:
            n["rationale"] = r["rationale"]
        if r["definition_file"]:
            n["definition_file"] = r["definition_file"]
            n["definition_location"] = r["definition_location"]
        for k, v in extra.items():
            n.setdefault(k, v)
        nodes_out.append(_canonical(n, ("id", "label")))

    links_out = []
    for e in db.edges():
        s, d = int(e["src"]), int(e["dst"])
        if s not in id_to_slug or d not in id_to_slug:
            continue
        link = {
            "source": id_to_slug[s],
            "target": id_to_slug[d],
            "relation": e["relation"],
            "confidence": e["confidence"],
            "confidence_score": e["confidence_score"]
            if e["confidence_score"] is not None
            else _CONF_SCORE.get(e["confidence"], 0.55),
            "source_file": e["source_file"],
            "source_location": e["source_location"],
            "weight": e["weight"] if e["weight"] is not None else 1.0,
        }
        if e["context"]:
            link["context"] = e["context"]
        if e["deferred"]:
            link["deferred"] = True
        if e["type_only"]:
            link["type_only"] = True
        links_out.append(_canonical(link, ("source", "target", "relation")))

    nodes_out.sort(key=_sort_key)
    links_out.sort(key=_sort_key)

    data = {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": nodes_out,
        "links": links_out,
        "hyperedges": [],
    }
    head = db.get_meta("built_at_commit")
    if head:
        data["built_at_commit"] = head
    return data


def write_graph_json(db: Db, out: Path) -> Path:
    data = build_graph_json(db)
    out.mkdir(parents=True, exist_ok=True)
    target = out / GRAPH_JSON_NAME
    fd, tmp = tempfile.mkstemp(dir=out, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        atomic_replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target
