"""Pull-request impact via the graph (``codegraph prs``).

Thin wrapper over the ``gh`` CLI: for each open PR, map its changed files to graph
nodes and run the same reverse-dependency traversal ``affected`` uses, so you see
*what each PR can break* ranked by blast radius — graphify's PR-dashboard tools
without a hosted service.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import defaultdict

from .db import Db


class GhUnavailable(RuntimeError):
    pass


def _gh(*args: str) -> object:
    if not shutil.which("gh"):
        raise GhUnavailable("the GitHub CLI (`gh`) is not on PATH")
    try:
        out = subprocess.run(["gh", *args], capture_output=True, text=True,
                             timeout=30, encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GhUnavailable(str(e)) from e
    if out.returncode != 0:
        raise GhUnavailable(out.stderr.strip()[:300] or "gh failed")
    return json.loads(out.stdout) if out.stdout.strip() else []


def list_prs(limit: int = 30) -> list[dict]:
    rows = _gh("pr", "list", "--state", "open", "--limit", str(limit),
               "--json", "number,title,headRefName,author,additions,deletions,files")
    prs = []
    for r in rows:  # type: ignore[union-attr]
        prs.append({
            "number": r["number"], "title": r["title"],
            "branch": r.get("headRefName"),
            "author": (r.get("author") or {}).get("login"),
            "files": [f["path"] for f in r.get("files", [])],
            "churn": r.get("additions", 0) + r.get("deletions", 0),
        })
    return prs


def _nodes_in_files(db: Db, files: set[str]) -> list[int]:
    if not files:
        return []
    q = ",".join("?" for _ in files)
    return [int(r["id"]) for r in db.conn.execute(
        f"SELECT id FROM nodes WHERE source_file IN ({q})", list(files)
    ).fetchall()]


def pr_impact(db: Db, number: int, *, depth: int = 3) -> dict:
    r = _gh("pr", "view", str(number), "--json", "number,title,files,headRefName")
    files = {f["path"] for f in r.get("files", [])}  # type: ignore[union-attr]
    by_id = {int(x["id"]): x for x in db.nodes()}
    rev: dict[int, list[int]] = defaultdict(list)
    for e in db.edges():
        if e["relation"] in ("calls", "references", "imports_from", "implements",
                             "inherits", "method", "contains"):
            rev[int(e["dst"])].append(int(e["src"]))

    changed = _nodes_in_files(db, files)
    seen = set(changed)
    frontier = list(changed)
    for _ in range(depth):
        nxt = []
        for n in frontier:
            for s in rev.get(n, []):
                if s not in seen:
                    seen.add(s)
                    nxt.append(s)
        frontier = nxt

    hit_files = defaultdict(int)
    for n in seen:
        if n in by_id and by_id[n]["source_file"]:
            hit_files[by_id[n]["source_file"]] += 1
    external = sorted(
        (f for f in hit_files if f not in files),
        key=lambda f: -hit_files[f],
    )
    return {
        "number": r["number"], "title": r["title"],  # type: ignore[index]
        "changed_files": sorted(files),
        "changed_nodes": len(changed),
        "affected_nodes": len(seen) - len(changed),
        "affected_files": external,
        "blast_radius": len(external),
    }


def triage(db: Db, limit: int = 30, *, depth: int = 3) -> list[dict]:
    out = []
    for pr in list_prs(limit):
        try:
            imp = pr_impact(db, pr["number"], depth=depth)
        except GhUnavailable:
            continue
        out.append({**pr, "blast_radius": imp["blast_radius"],
                    "affected_files": imp["affected_files"][:10]})
    out.sort(key=lambda p: -p["blast_radius"])
    return out
