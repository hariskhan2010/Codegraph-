"""Semantic annotation pass.

The LLM is shown a file's text plus the AST nodes codegraph already extracted for
it, and asked to (a) attach a one-line ``rationale`` and ``concepts`` to those
nodes and (b) name ``AMBIGUOUS`` edges the AST could not prove. **The model never
returns a node id** — every annotation is keyed by ``(label, line)`` and matched
back to an existing node here. Unmatched annotations are dropped and counted.

This is the inversion of graphify's semantic pass, where the LLM must mint
structural node ids that byte-match the AST extractor's — the source of its
ghost-node bug cluster.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .db import Db
from .llm import Backend, LLMError, complete

_SYSTEM = """You annotate an existing code graph. You are given one source file \
and the list of symbols already extracted from it. Return ONLY a JSON object:

{
  "annotations": [
    {"label": "<exact label from the list>", "line": <int>,
     "rationale": "<=15 words: what this symbol is for",
     "concepts": ["<short domain concept>", ...]}
  ],
  "ambiguous_edges": [
    {"src": "<label>", "dst": "<label>", "relation": "calls|references|uses",
     "why": "<=12 words"}
  ]
}

Rules: use only labels from the provided list. Omit a symbol rather than guess.
`ambiguous_edges` are for real relationships the caller/callee is dynamic or
duck-typed so a parser cannot be certain — do not restate obvious direct calls.
No prose outside the JSON."""

_MAX_FILE_CHARS = 18_000
_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _parse(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1]
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = _JSON_RE.search(text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def _line_of(loc: str | None) -> int:
    if not loc:
        return 0
    m = re.match(r"L(\d+)", loc)
    return int(m.group(1)) if m else 0


def annotate_file(db: Db, backend: Backend, rel: str, src_text: str) -> dict:
    rows = db.conn.execute(
        "SELECT id, label, kind, source_location FROM nodes "
        "WHERE source_file=? AND origin IN ('ast','semantic') AND kind!='file'",
        (rel,),
    ).fetchall()
    if not rows:
        return {"annotated": 0, "amb_edges": 0, "dropped": 0}

    listing = "\n".join(
        f"- {r['label']}  (line {_line_of(r['source_location'])}, {r['kind']})"
        for r in rows
    )
    body = src_text[:_MAX_FILE_CHARS]
    user = f"FILE: {rel}\n\nSYMBOLS:\n{listing}\n\nSOURCE:\n```\n{body}\n```"
    raw = complete(backend, _SYSTEM, user)
    data = _parse(raw)

    # index existing nodes by normalized label -> [(id, line)]
    idx: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        key = (r["label"] or "").strip(".()").lower()
        idx.setdefault(key, []).append((int(r["id"]), _line_of(r["source_location"])))

    def match(label: str, line) -> int | None:
        key = str(label).strip(".()").lower()
        cands = idx.get(key, [])
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0][0]
        try:
            ln = int(line)
        except (TypeError, ValueError):
            return cands[0][0]
        return min(cands, key=lambda c: abs(c[1] - ln))[0]

    annotated = dropped = amb = 0
    with db.tx() as c:
        # reset this file's prior semantic contribution (idempotent)
        c.execute(
            "UPDATE nodes SET rationale=NULL WHERE source_file=? AND origin='ast'",
            (rel,),
        )
        c.execute("DELETE FROM edges WHERE source_file=? AND evidence='llm'", (rel,))

        for a in data.get("annotations", []):
            nid = match(a.get("label", ""), a.get("line"))
            if nid is None:
                dropped += 1
                continue
            rationale = str(a.get("rationale", "")).strip()[:300] or None
            concepts = [str(x)[:40] for x in a.get("concepts", []) if x][:6]
            extra_row = c.execute("SELECT extra FROM nodes WHERE id=?", (nid,)).fetchone()
            extra = json.loads(extra_row["extra"]) if extra_row and extra_row["extra"] else {}
            if concepts:
                extra["concepts"] = concepts
            c.execute(
                "UPDATE nodes SET rationale=?, extra=? WHERE id=?",
                (rationale, json.dumps(extra, sort_keys=True) if extra else None, nid),
            )
            if rationale:
                annotated += 1

        for e in data.get("ambiguous_edges", []):
            s = match(e.get("src", ""), None)
            d = match(e.get("dst", ""), None)
            if s is None or d is None or s == d:
                continue
            rel_name = e.get("relation", "references")
            if rel_name not in ("calls", "references", "uses"):
                rel_name = "references"
            cur = c.execute(
                "INSERT OR IGNORE INTO edges(src,dst,relation,confidence,"
                "confidence_score,context,source_file,evidence) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (s, d, rel_name, "AMBIGUOUS", 0.2, "call", rel, "llm"),
            )
            if cur.rowcount > 0:
                amb += 1

    return {"annotated": annotated, "amb_edges": amb, "dropped": dropped}


_NAME_SYSTEM = """You label the communities (clusters) of a code graph. For each \
community you get its id and a sample of member symbols with their files. Return \
ONLY JSON: {"names": {"<id>": "<2-5 word Title Case label naming what this \
cluster does>"}}. Base the label on the domain role (e.g. "Stripe Checkout Flow", \
"JWT Auth & Sessions", "Candle Time Arithmetic"), not on the language. A cluster \
that is only tests -> "<subject> Tests". No prose outside the JSON."""


def name_communities(db: Db, backend: Backend, *, progress=None) -> dict:
    """Replace the heuristic community labels with short LLM-written ones
    (graphify's community-naming pass). Safe to skip — falls back to whatever
    :mod:`codegraph.cluster` produced."""
    comms = db.conn.execute(
        "SELECT id, label FROM communities ORDER BY id"
    ).fetchall()
    if not comms:
        return {"named": 0}
    sizes = {
        r["community"]: r["n"] for r in db.conn.execute(
            "SELECT community, COUNT(*) n FROM nodes WHERE community IS NOT NULL "
            "GROUP BY community"
        ).fetchall()
    }
    lines: list[str] = []
    for c in comms:
        if sizes.get(c["id"], 0) < 3:
            continue
        members = db.conn.execute(
            "SELECT label, source_file FROM nodes WHERE community=? "
            "AND kind NOT IN ('file','stub') ORDER BY degree DESC LIMIT 12",
            (c["id"],),
        ).fetchall()
        sample = "; ".join(f"{m['label']} ({m['source_file']})" for m in members)
        lines.append(f'#{c["id"]} [{sizes.get(c["id"], 0)} nodes]: {sample}')
    if not lines:
        return {"named": 0}

    raw = complete(backend, _NAME_SYSTEM, "\n".join(lines))
    data = _parse(raw)
    names = data.get("names") or {}
    n = 0
    with db.tx() as c:
        for cid_s, name in names.items():
            try:
                cid = int(cid_s)
            except (TypeError, ValueError):
                continue
            name = str(name).strip()[:60]
            if not name:
                continue
            c.execute("UPDATE communities SET label=? WHERE id=?", (name, cid))
            c.execute("UPDATE nodes SET community_name=? WHERE community=?", (name, cid))
            n += 1
    if progress:
        progress({"communities_named": n})
    return {"named": n}


def run(db: Db, root: Path, backend: Backend, *, force: bool = False,
        progress=None) -> dict:
    """Annotate every code file whose ``semantic_hash`` is empty (or all, if
    ``force``). Never raises for a single-file failure — logs and continues."""
    totals = {"files": 0, "annotated": 0, "amb_edges": 0, "dropped": 0, "failed": 0}

    # if the annotation prompt changed since last run, every file is stale
    import hashlib

    fp = hashlib.sha256((_SYSTEM + "\x00" + backend.model).encode()).hexdigest()[:16]
    if db.get_meta("semantic_prompt_fp") != fp:
        force = True
    db.set_meta("semantic_prompt_fp", fp)
    db.conn.commit()

    files = db.conn.execute(
        "SELECT path, content_sha256, semantic_hash FROM files "
        "WHERE file_type='code' AND status='present'"
    ).fetchall()
    for f in files:
        if not force and f["semantic_hash"] and f["semantic_hash"] == f["content_sha256"]:
            continue
        fp = root / f["path"]
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            r = annotate_file(db, backend, f["path"], text)
        except LLMError as e:
            totals["failed"] += 1
            if progress:
                progress({"file": f["path"], "error": str(e)[:120]})
            continue
        db.conn.execute(
            "UPDATE files SET semantic_hash=? WHERE path=?",
            (f["content_sha256"], f["path"]),
        )
        db.conn.commit()
        totals["files"] += 1
        for k in ("annotated", "amb_edges", "dropped"):
            totals[k] += r[k]
        if progress:
            progress({"file": f["path"], **r})
    db.set_meta("semantic_backend", backend.name)
    db.set_meta("semantic_at", str(time.time()))
    db.conn.commit()
    return totals
