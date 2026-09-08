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


def _file_symbols(db: Db, rel: str):
    return db.conn.execute(
        "SELECT id, label, kind, source_location FROM nodes "
        "WHERE source_file=? AND origin IN ('ast','semantic') AND kind!='file'",
        (rel,),
    ).fetchall()


def annotate_file(db: Db, backend: Backend, rel: str, src_text: str) -> dict:
    rows = _file_symbols(db, rel)
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
    return apply_file_annotations(db, rel, data)


def apply_file_annotations(db: Db, rel: str, data: dict) -> dict:
    """Ingest one file's ``{annotations, ambiguous_edges}`` payload (from any
    source — an API model, ``claude-cli``, or the ``/codegraph`` skill)."""
    rows = _file_symbols(db, rel)
    if not rows:
        return {"annotated": 0, "amb_edges": 0, "dropped": 0}

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


def community_digest(db: Db, *, min_size: int = 3, limit: int = 14) -> list[dict]:
    """One entry per non-trivial community: id, size, current heuristic label, and
    a sample of its most-connected members. Feeds both the LLM naming pass and the
    ``/codegraph`` skill's naming step."""
    sizes = {
        r["community"]: r["n"] for r in db.conn.execute(
            "SELECT community, COUNT(*) n FROM nodes WHERE community IS NOT NULL "
            "GROUP BY community"
        ).fetchall()
    }
    out: list[dict] = []
    for c in db.conn.execute("SELECT id, label FROM communities ORDER BY id").fetchall():
        if sizes.get(c["id"], 0) < min_size:
            continue
        members = db.conn.execute(
            "SELECT label, kind, source_file FROM nodes WHERE community=? "
            "AND kind NOT IN ('file','stub') ORDER BY degree DESC LIMIT ?",
            (c["id"], limit),
        ).fetchall()
        out.append({
            "id": c["id"], "size": sizes.get(c["id"], 0), "current_label": c["label"],
            "members": [{"label": m["label"], "kind": m["kind"],
                         "file": m["source_file"]} for m in members],
        })
    return out


def apply_community_names(db: Db, names: dict) -> int:
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
    return n


def name_communities(db: Db, backend: Backend, *, progress=None) -> dict:
    """Replace the heuristic community labels with short LLM-written ones
    (graphify's community-naming pass). Safe to skip — falls back to whatever
    :mod:`codegraph.cluster` produced."""
    digest = community_digest(db)
    if not digest:
        return {"named": 0}
    lines = [
        f'#{d["id"]} [{d["size"]} nodes]: '
        + "; ".join(f'{m["label"]} ({m["file"]})' for m in d["members"])
        for d in digest
    ]
    data = _parse(complete(backend, _NAME_SYSTEM, "\n".join(lines)))
    n = apply_community_names(db, data.get("names") or {})
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


# --------------------------------------------------------------------------- #
# skill-driven semantic pass — the /codegraph skill (the session's own model)
# does the annotation for free, graphify-style, instead of a separate backend.
# --------------------------------------------------------------------------- #

REQUEST_NAME = "semantic-request.json"
RESPONSE_NAME = "semantic-response.json"
CHUNK_DIR = "semantic"           # holds request-NNN.json / response-NNN.json
SKILL_CHUNK_FILES = 25           # files/chunk once the skill fans out to subagents

_SKILL_INSTRUCTIONS = (
    "You are the semantic pass for codegraph. For every file in `files`, write a "
    "one-line `rationale` (<=15 words, what the symbol is for) for each of its "
    "`symbols`, using only labels from that list, and list any `ambiguous_edges` "
    "(dynamic/duck-typed calls a parser can't see). For every entry in "
    "`communities`, write a 2-5 word Title Case `name` for its domain role "
    "(\"Stripe Checkout Flow\", \"JWT Auth & Sessions\"); a tests-only cluster -> "
    "\"<subject> Tests\". Write the result to " + RESPONSE_NAME + " as "
    '{"annotations": {"<file>": [{"label","line","rationale","concepts"}]}, '
    '"ambiguous_edges": {"<file>": [{"src","dst","relation","why"}]}, '
    '"community_names": {"<id>": "<name>"}}. Then run '
    "`codegraph apply-semantic <path>`."
)

_CHUNK_INSTRUCTIONS = (
    "You are ONE parallel worker of the codegraph semantic pass. Annotate only "
    "the files in this chunk's `files`: a one-line `rationale` (<=15 words) per "
    "symbol, using only labels from each file's `symbols` list, plus any "
    "`ambiguous_edges`. Write ONLY this chunk's result to `" + CHUNK_DIR +
    "/response-<NNN>.json` (same NNN as this request) as "
    '{"annotations": {"<file>": [{"label","line","rationale","concepts"}]}, '
    '"ambiguous_edges": {"<file>": [{"src","dst","relation","why"}]}}. '
    "Do not touch other chunks or the communities file."
)

_NAMING_INSTRUCTIONS = (
    "Name every community for its domain role: a 2-5 word Title Case label "
    "(\"Stripe Checkout Flow\", \"JWT Auth & Sessions\"); a tests-only cluster -> "
    "\"<subject> Tests\". Write `" + CHUNK_DIR + "/communities-response.json` as "
    '{"community_names": {"<id>": "<name>"}}.'
)

_DISPATCH_INSTRUCTIONS = (
    "This request is fanned out. Dispatch one general-purpose subagent per "
    "`" + CHUNK_DIR + "/request-NNN.json` IN A SINGLE MESSAGE (they run in "
    "parallel); give each the text of its request file and have it write "
    "`" + CHUNK_DIR + "/response-NNN.json`. Also handle `" + CHUNK_DIR +
    "/communities.json` (one more subagent, or do it yourself) -> `" + CHUNK_DIR +
    "/communities-response.json`. When all response files exist, run "
    "`codegraph apply-semantic <path>` — it merges them."
)


def _collect_payload(db: Db, root: Path, *, max_files: int, max_chars: int,
                     force: bool) -> list[dict]:
    files = db.conn.execute(
        "SELECT path, content_sha256, semantic_hash FROM files "
        "WHERE file_type='code' AND status='present' ORDER BY path"
    ).fetchall()
    payload: list[dict] = []
    for f in files:
        if not force and f["semantic_hash"] and f["semantic_hash"] == f["content_sha256"]:
            continue
        rows = _file_symbols(db, f["path"])
        if not rows:
            continue
        try:
            text = (root / f["path"]).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        payload.append({
            "file": f["path"],
            "symbols": [{"label": r["label"], "line": _line_of(r["source_location"]),
                         "kind": r["kind"]} for r in rows],
            "source": text[:max_chars],
        })
        if len(payload) >= max_files:
            break
    return payload


def write_request(db: Db, root: Path, *, max_files: int = 400,
                  max_chars: int = _MAX_FILE_CHARS, force: bool = False,
                  chunk_files: int = 0) -> dict:
    """Emit the skill's semantic request. Does not call any model.

    ``chunk_files == 0`` (default) writes a single ``semantic-request.json``.
    ``chunk_files > 0`` and enough files fans the work out: one
    ``semantic/request-NNN.json`` per group plus ``semantic/communities.json``,
    so the ``/codegraph`` skill can dispatch a subagent per chunk (graphify's
    parallel model) while codegraph still owns the chunking and the merge.
    """
    from .config import out_dir

    payload_files = _collect_payload(
        db, root, max_files=max_files, max_chars=max_chars, force=force)
    communities = community_digest(db)
    out = out_dir(root)
    out.mkdir(parents=True, exist_ok=True)

    if chunk_files and len(payload_files) > chunk_files:
        cdir = out / CHUNK_DIR
        cdir.mkdir(parents=True, exist_ok=True)
        # clear any stale chunk files from a prior run
        for old in cdir.glob("*.json"):
            old.unlink()
        groups = [payload_files[i:i + chunk_files]
                  for i in range(0, len(payload_files), chunk_files)]
        n = len(groups)
        for i, g in enumerate(groups, 1):
            (cdir / f"request-{i:03d}.json").write_text(json.dumps({
                "instructions": _CHUNK_INSTRUCTIONS,
                "chunk": f"{i}/{n}", "root": str(root), "files": g,
            }, indent=1, ensure_ascii=False), encoding="utf-8")
        (cdir / "communities.json").write_text(json.dumps({
            "instructions": _NAMING_INSTRUCTIONS, "communities": communities,
        }, indent=1, ensure_ascii=False), encoding="utf-8")
        index = {
            "instructions": _DISPATCH_INSTRUCTIONS, "root": str(root),
            "chunked": True, "chunk_dir": CHUNK_DIR, "chunks": n,
            "files": len(payload_files), "communities": len(communities),
        }
        (out / REQUEST_NAME).write_text(
            json.dumps(index, indent=1, ensure_ascii=False), encoding="utf-8")
        return {"files": len(payload_files), "communities": len(communities),
                "chunks": n, "path": str(out / REQUEST_NAME),
                "chunk_dir": str(cdir)}

    req = {
        "instructions": _SKILL_INSTRUCTIONS,
        "root": str(root),
        "files": payload_files,
        "communities": communities,
    }
    target = out / REQUEST_NAME
    target.write_text(json.dumps(req, indent=1, ensure_ascii=False), encoding="utf-8")
    return {"files": len(payload_files), "communities": len(communities),
            "chunks": 0, "path": str(target)}


def _merge_chunk_responses(cdir: Path) -> dict | None:
    """Fold ``semantic/response-*.json`` + ``communities-response.json`` into one
    ``{annotations, ambiguous_edges, community_names}`` payload."""
    resp = sorted(cdir.glob("response-*.json"))
    names_file = cdir / "communities-response.json"
    if not resp and not names_file.exists():
        return None
    ann: dict[str, list] = {}
    amb: dict[str, list] = {}
    names: dict[str, str] = {}
    for p in resp:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for f, items in (d.get("annotations") or {}).items():
            ann.setdefault(f, []).extend(items)
        for f, items in (d.get("ambiguous_edges") or {}).items():
            amb.setdefault(f, []).extend(items)
        names.update(d.get("community_names") or {})
    if names_file.exists():
        try:
            names.update(json.loads(names_file.read_text(encoding="utf-8"))
                         .get("community_names") or {})
        except (OSError, ValueError):
            pass
    return {"annotations": ann, "ambiguous_edges": amb, "community_names": names,
            "_chunks": len(resp)}


def apply_response(db: Db, root: Path, response: dict | str | Path | None = None) -> dict:
    """Ingest the skill's semantic response(s) back into the graph.

    Accepts an in-memory dict, a single response file, or — when ``response`` is
    ``None`` or a directory — the fanned-out ``semantic/response-*.json`` set,
    falling back to a single ``semantic-response.json``.
    """
    from .config import out_dir

    merged_chunks = 0
    if not isinstance(response, dict):
        p = Path(response) if response is not None else None
        cdir = out_dir(root) / CHUNK_DIR
        if p is not None and p.is_dir():
            cdir, p = p, None
        if p is not None and p.is_file():
            response = json.loads(p.read_text(encoding="utf-8"))
        else:
            m = _merge_chunk_responses(cdir)
            if m is not None:
                merged_chunks = m.pop("_chunks", 0)
                response = m
            else:
                single = out_dir(root) / RESPONSE_NAME
                if not single.exists():
                    raise FileNotFoundError(
                        f"no semantic response at {single} or {cdir}/response-*.json")
                response = json.loads(single.read_text(encoding="utf-8"))

    ann = response.get("annotations") or {}
    amb = response.get("ambiguous_edges") or {}
    totals = {"files": 0, "annotated": 0, "amb_edges": 0, "dropped": 0}
    for rel in set(ann) | set(amb):
        data = {"annotations": ann.get(rel, []),
                "ambiguous_edges": amb.get(rel, [])}
        r = apply_file_annotations(db, rel, data)
        totals["files"] += 1
        for k in ("annotated", "amb_edges", "dropped"):
            totals[k] += r[k]
        row = db.conn.execute(
            "SELECT content_sha256 FROM files WHERE path=?", (rel,)
        ).fetchone()
        if row:
            db.conn.execute("UPDATE files SET semantic_hash=? WHERE path=?",
                            (row["content_sha256"], rel))
    totals["communities_named"] = apply_community_names(
        db, response.get("community_names") or {})
    totals["merged_chunks"] = merged_chunks
    db.set_meta("semantic_backend", "skill")
    db.set_meta("semantic_at", str(time.time()))
    db.conn.commit()
    return totals
