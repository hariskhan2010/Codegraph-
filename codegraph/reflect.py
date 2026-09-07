"""Work-memory feedback loop — ``save-result`` and ``reflect``.

graphify does this with markdown files in ``graphify-out/memory/`` parsed by a
hand-rolled YAML subset; codegraph uses the ``queries`` table and a ``SELECT``.
``reflect`` aggregates outcomes with exponential time-decay into
"preferred sources" / "known dead ends", stashed in ``meta`` and surfaced by the
report and ``explain``.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from .db import Db

_OUTCOMES = ("useful", "dead_end", "corrected")


def save_result(
    db: Db,
    question: str,
    answer: str,
    *,
    outcome: str | None = None,
    correction: str | None = None,
    source_nodes: list[str] | None = None,
) -> None:
    if outcome and outcome not in _OUTCOMES:
        raise ValueError(f"outcome must be one of {_OUTCOMES}")
    db.log_query(
        ts=time.time(), kind="save-result", question=question,
        corpus=db.get_meta("root"), nodes_returned=len(source_nodes or []),
        duration_ms=None, outcome=outcome, correction=correction,
        source_nodes=json.dumps(source_nodes or []),
    )


def _decay(ts: float, now: float, half_life_days: float) -> float:
    age_days = max(0.0, (now - ts) / 86400.0)
    return 0.5 ** (age_days / half_life_days)


def reflect(db: Db, *, half_life_days: float = 30.0, min_corroboration: int = 2) -> dict:
    now = time.time()
    rows = db.conn.execute(
        "SELECT ts, question, outcome, correction, source_nodes FROM queries "
        "WHERE outcome IS NOT NULL ORDER BY ts"
    ).fetchall()

    per_node: dict[str, dict] = {}
    dead_ends: list[dict] = []
    corrections: list[dict] = []
    for r in rows:
        sign = 1 if r["outcome"] == "useful" else -1
        w = _decay(r["ts"], now, half_life_days)
        nodes = json.loads(r["source_nodes"] or "[]")
        if r["outcome"] in ("dead_end", "corrected") and not nodes:
            dead_ends.append({"question": r["question"]})
        if r["correction"]:
            corrections.append({"question": r["question"], "correction": r["correction"]})
        for n in nodes:
            d = per_node.setdefault(n, {"score": 0.0, "pos": 0, "neg": 0, "last": r["ts"]})
            d["score"] += sign * w
            d["pos" if sign > 0 else "neg"] += 1
            d["last"] = max(d["last"], r["ts"])

    preferred, tentative, contested = [], [], []
    for name, d in sorted(per_node.items(), key=lambda kv: -kv[1]["score"]):
        last = datetime.fromtimestamp(d["last"], timezone.utc).date().isoformat()
        entry = {"node": name, "pos": d["pos"], "neg": d["neg"],
                 "score": round(d["score"], 3), "last": last}
        if d["pos"] and d["neg"]:
            contested.append(entry)
        elif d["pos"] and d["neg"] == 0:
            (preferred if d["pos"] >= min_corroboration else tentative).append(entry)
        elif d["neg"] and not d["pos"]:
            dead_ends.append({"node": name, **entry})

    result = {
        "generated_at": now,
        "total": len(rows),
        "preferred": preferred,
        "tentative": tentative,
        "contested": contested,
        "dead_ends": dead_ends,
        "corrections": corrections,
    }
    db.set_meta("reflection", json.dumps(result))
    db.conn.commit()
    return result


def load_reflection(db: Db) -> dict:
    return json.loads(db.get_meta("reflection") or "{}")
