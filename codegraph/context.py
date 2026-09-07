"""``codegraph context <symbol>`` — emit exactly the code an agent needs.

The sharpest form of the token-economics argument in ``PLAN.md``: instead of the
agent grepping a symbol, opening its file, then opening every caller/callee file,
``context`` returns one bounded blob — the target symbol's full body plus the
*signatures* (not bodies) of everything that calls it and everything it calls,
each with a precise ``file:line`` anchor. Zero exploratory reads.
"""

from __future__ import annotations

from pathlib import Path

from .db import Db
from .query import _resolve_node

_CALL_RELS = ("calls", "references")
_SIG_STOP = (":", "{", "=>", "(", ")")


def _root(db: Db) -> Path:
    return Path(db.get_meta("root") or ".")


def _loc_range(loc: str | None) -> tuple[int, int] | None:
    if not loc:
        return None
    nums = [int(x) for x in loc.replace("L", " ").replace("-", " ").split() if x.isdigit()]
    if not nums:
        return None
    return nums[0], (nums[1] if len(nums) > 1 else nums[0])


def _read_lines(root: Path, rel: str) -> list[str] | None:
    try:
        return (root / rel).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _slice(root: Path, rel: str, loc: str | None, *, max_lines: int) -> str:
    lines = _read_lines(root, rel)
    rng = _loc_range(loc)
    if lines is None or rng is None:
        return ""
    a, b = rng
    b = min(b, a + max_lines - 1)
    body = lines[a - 1 : b]
    return "\n".join(f"{a + i:>5} {ln}" for i, ln in enumerate(body))


def _signature(root: Path, rel: str, loc: str | None) -> str:
    """First non-trivial line(s) of a definition, up to the body opener."""
    lines = _read_lines(root, rel)
    rng = _loc_range(loc)
    if lines is None or rng is None:
        return ""
    a = rng[0]
    out: list[str] = []
    for ln in lines[a - 1 : a - 1 + 4]:
        out.append(ln.rstrip())
        stripped = ln.strip()
        if stripped.endswith((":", "{")) or "=>" in stripped or (
            stripped.endswith(")") and len(out) > 1
        ):
            break
    return " ".join(s.strip() for s in out).rstrip("{").strip()


def context(db: Db, label: str, *, callers: int = 8, callees: int = 10,
            body_lines: int = 60) -> str:
    nid, err = _resolve_node(db, label)
    if err:
        return err
    root = _root(db)
    by_id = {int(r["id"]): r for r in db.nodes()}
    r = by_id[nid]

    out: list[str] = []
    loc = r["source_location"]
    out.append(f"# {r['label']}  —  {r['source_file']}:{loc}"
               + (f"  [{r['kind']}]" if r["kind"] else ""))
    if r["rationale"]:
        out.append(f"# {r['rationale']}")
    out.append("")

    body = _slice(root, r["source_file"], loc, max_lines=body_lines)
    if body:
        out.append("## definition")
        out.append(body)
        out.append("")

    outgoing = db.conn.execute(
        "SELECT DISTINCT dst, relation FROM edges WHERE src=? AND relation IN "
        f"({','.join('?' for _ in _CALL_RELS)}) ORDER BY relation, dst",
        (nid, *_CALL_RELS),
    ).fetchall()
    incoming = db.conn.execute(
        "SELECT DISTINCT src, relation, source_location FROM edges WHERE dst=? AND relation IN "
        f"({','.join('?' for _ in _CALL_RELS)}) ORDER BY relation, src",
        (nid, *_CALL_RELS),
    ).fetchall()

    if incoming:
        out.append(f"## callers ({len(incoming)})")
        for e in incoming[:callers]:
            s = by_id.get(int(e["src"]))
            if not s:
                continue
            sig = _signature(root, s["source_file"], s["source_location"])
            site = e["source_location"] or s["source_location"]
            out.append(f"  {s['source_file']}:{site}  --{e['relation']}-->")
            if sig:
                out.append(f"      {sig}")
        if len(incoming) > callers:
            out.append(f"  … +{len(incoming) - callers} more")
        out.append("")

    if outgoing:
        out.append(f"## callees ({len(outgoing)})")
        for e in outgoing[:callees]:
            t = by_id.get(int(e["dst"]))
            if not t:
                continue
            sig = _signature(root, t["source_file"], t["source_location"])
            out.append(f"  {t['source_file']}:{t['source_location']}  --{e['relation']}-->  {t['label']}")
            if sig:
                out.append(f"      {sig}")
        if len(outgoing) > callees:
            out.append(f"  … +{len(outgoing) - callees} more")
        out.append("")

    return "\n".join(out).rstrip() + "\n"
