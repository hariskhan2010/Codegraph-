"""Optional embedding tier for synonym retrieval (``codegraph embed``).

FTS5 stemming + rapidfuzz vocab expansion already bridge morphology and typos.
Embeddings add the one thing they cannot: true synonyms (``authentication`` ->
``login`` / ``session`` / ``credential``). Opt-in because it needs a model.

Vectors are cached by text hash in ``vec_cache`` so re-running ``embed`` after an
``extract`` (which reassigns node PKs) costs zero API calls.
"""

from __future__ import annotations

import hashlib
import struct

from .db import Db
from .llm import Backend, LLMError, embed_texts

_F32 = "<%df"


def _pack(v: list[float]) -> bytes:
    return struct.pack(_F32 % len(v), *v)


def _unpack(b: bytes) -> list[float]:
    return list(struct.unpack(_F32 % (len(b) // 4), b))


def _text_for(row) -> str:
    parts = [row["label"] or "", row["norm_label"] or ""]
    if row["rationale"]:
        parts.append(row["rationale"])
    extra = row["extra"]
    if extra and '"concepts"' in extra:
        import json

        try:
            parts += json.loads(extra).get("concepts", [])
        except json.JSONDecodeError:
            pass
    return " ".join(p for p in parts if p).strip()


def _sha(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\x00{text}".encode()).hexdigest()


def build(db: Db, backend: Backend, *, progress=None) -> dict:
    rows = [r for r in db.nodes() if (r["kind"] or "") not in ("file", "module")]
    texts = {int(r["id"]): _text_for(r) for r in rows}
    texts = {nid: t for nid, t in texts.items() if t}

    cache: dict[str, bytes] = {}
    for r in db.conn.execute(
        "SELECT text_sha, dim, embedding FROM vec_cache WHERE model=?", (backend.model,)
    ).fetchall():
        cache[r["text_sha"]] = r["embedding"]

    need: list[tuple[int, str, str]] = []
    dim = None
    hits = 0
    for nid, text in texts.items():
        sha = _sha(backend.model, text)
        if sha in cache:
            hits += 1
        else:
            need.append((nid, text, sha))

    fresh: dict[str, list[float]] = {}
    if need:
        try:
            vecs = embed_texts(backend, [t for _, t, _ in need])
        except LLMError:
            raise
        for (_, _, sha), v in zip(need, vecs):
            fresh[sha] = v
            dim = len(v)

    with db.tx() as c:
        for sha, v in fresh.items():
            c.execute(
                "INSERT OR REPLACE INTO vec_cache(text_sha,model,dim,embedding) "
                "VALUES(?,?,?,?)",
                (sha, backend.model, len(v), _pack(v)),
            )
        c.execute("DELETE FROM node_vec")
        for nid, text in texts.items():
            sha = _sha(backend.model, text)
            blob = fresh.get(sha)
            raw = _pack(blob) if blob is not None else cache.get(sha)
            if raw is None:
                continue
            c.execute(
                "INSERT OR REPLACE INTO node_vec(node_id,dim,embedding) VALUES(?,?,?)",
                (nid, len(raw) // 4, raw),
            )
        db.set_meta("embed_model", backend.model)
        db.set_meta("embed_backend", backend.name)

    stored = db.conn.execute("SELECT COUNT(*) n FROM node_vec").fetchone()["n"]
    if progress:
        progress({"embedded": stored, "cache_hits": hits, "api_calls": len(need)})
    return {"nodes": len(texts), "stored": stored, "cache_hits": hits,
            "api_calls": len(need), "model": backend.model}


def _cos(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    return num  # both sides are L2-normalised at store time for openai; guard below


def nearest(db: Db, backend: Backend, query: str, *, k: int = 8,
            min_sim: float = 0.30) -> list[tuple[int, float]]:
    """Node ids whose embedding is closest to ``query``'s. Empty if no vectors."""
    rows = db.conn.execute("SELECT node_id, embedding FROM node_vec").fetchall()
    if not rows:
        return []
    try:
        qv = embed_texts(backend, [query])[0]
    except LLMError:
        return []
    import math

    qn = math.sqrt(sum(x * x for x in qv)) or 1.0
    qv = [x / qn for x in qv]

    scored: list[tuple[int, float]] = []
    for r in rows:
        v = _unpack(r["embedding"])
        if len(v) != len(qv):
            continue
        vn = math.sqrt(sum(x * x for x in v)) or 1.0
        sim = sum(x * y for x, y in zip(qv, v)) / vn
        if sim >= min_sim:
            scored.append((int(r["node_id"]), sim))
    scored.sort(key=lambda t: -t[1])
    return scored[:k]
