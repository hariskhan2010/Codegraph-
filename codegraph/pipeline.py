"""End-to-end orchestration: detect -> extract-ast -> resolve -> cluster ->
analyze -> render. This is the code that graphify makes an LLM do by hand.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import __version__
from .config import db_path, out_dir
from .db import Db
from .detect import detect
from .extract import extract_file


def _backup_if_protected(db: Db, root: Path) -> str | None:
    """Copy the current artifacts to ``codegraph-out/<date>/`` before a rebuild
    overwrites a graph that cost real LLM tokens (semantic rationale or
    embeddings present)."""
    import shutil
    from datetime import date

    protected = db.conn.execute(
        "SELECT 1 FROM nodes WHERE rationale IS NOT NULL AND rationale != '' LIMIT 1"
    ).fetchone() or db.conn.execute(
        "SELECT 1 FROM node_vec LIMIT 1"
    ).fetchone()
    if not protected:
        return None
    out = out_dir(root)
    artifacts = [out / "graph.json", out / "GRAPH_REPORT.md", out / "graph.html"]
    if not any(a.exists() for a in artifacts):
        return None
    dest = out / date.today().isoformat()
    dest.mkdir(parents=True, exist_ok=True)
    for a in artifacts:
        if a.exists():
            shutil.copy2(a, dest / a.name)
    return str(dest)


def _git_head(root: Path) -> str | None:
    head = root / ".git" / "HEAD"
    try:
        ref = head.read_text().strip()
        if ref.startswith("ref: "):
            p = root / ".git" / ref[5:]
            return p.read_text().strip()[:40] if p.exists() else None
        return ref[:40]
    except OSError:
        return None


def _index_one(db: Db, f, root: Path) -> dict:
    src = f.abs_path.read_bytes()
    res = extract_file(f.rel, src, f.lang)
    counts = db.replace_file(
        f.rel, res.nodes, res.edges, raw_calls=res.raw_refs, origin="ast"
    )
    db.upsert_file(
        path=f.rel, abs_identity=f.abs_path.resolve().as_posix(),
        file_type=f.file_type, lang=f.lang, content_sha256=f.content_sha256,
        ast_hash=f.content_sha256, semantic_hash="",
        mtime=f.mtime, seen=time.time(), status="present",
    )
    return {"file": f.rel, "error": res.error, **counts}


def _index_doc(db: Db, f, root: Path) -> dict:
    from .extract.docs import extract_doc

    text = f.abs_path.read_text(encoding="utf-8", errors="replace")
    res = extract_doc(f.rel, text, f.abs_path.suffix.lower())
    counts = db.replace_file(f.rel, res.nodes, res.edges, raw_calls=(), origin="ast")
    db.upsert_file(
        path=f.rel, abs_identity=f.abs_path.resolve().as_posix(),
        file_type=f.file_type, lang="markdown", content_sha256=f.content_sha256,
        ast_hash=f.content_sha256, semantic_hash="",
        mtime=f.mtime, seen=time.time(), status="present",
    )
    return {"file": f.rel, "error": res.error, **counts}


def extract(
    root: Path | str,
    *,
    force: bool = False,
    semantic: str | None = "auto",
    semantic_force: bool = False,
    docs: bool = True,
    scip: str | Path | None = "auto",
    lsp: bool = False,
    progress=None,
) -> dict:
    """Full build. Re-indexes every code file, then resolves, (optionally)
    annotates with an LLM, clusters, and renders.

    ``semantic``: ``"auto"`` picks the first available backend; ``"none"`` /
    ``None`` skips the LLM pass; any other value is a backend name
    (``anthropic``, ``openai``, ``gemini``, ``ollama``, ``claude-cli``, ``mock``).
    """
    root = Path(root).resolve()
    out_dir(root).mkdir(parents=True, exist_ok=True)
    db = Db(db_path(root))
    backed_up = _backup_if_protected(db, root)
    db.set_meta("root", root.as_posix())
    db.set_meta("codegraph_version", __version__)
    head = _git_head(root)
    if head:
        db.set_meta("built_at_commit", head)

    det = detect(root)
    seen: set[str] = set()
    results: list[dict] = []
    to_index = list(det.code)
    if docs:
        to_index += [f for f in det.files if f.file_type in ("document", "paper")]
    for f in to_index:
        seen.add(f.rel)
        prev = db.file_row(f.rel)
        if not force and prev and prev["ast_hash"] == f.content_sha256:
            continue
        r = (_index_doc if f.file_type in ("document", "paper") else _index_one)(db, f, root)
        results.append(r)
        if progress:
            progress(r)

    # mark vanished files deleted (and drop their rows). Files under the output
    # directory are ingested sources (`codegraph add`), not walked — never prune.
    _ingested_prefix = out_dir(root).name + "/"
    for row in db.all_files(status="present"):
        if row["path"] not in seen and not row["path"].startswith(_ingested_prefix):
            with db.tx() as c:
                c.execute("DELETE FROM nodes WHERE source_file=?", (row["path"],))
                c.execute("DELETE FROM edges WHERE source_file=?", (row["path"],))
                c.execute("DELETE FROM raw_refs WHERE source_file=?", (row["path"],))
                c.execute("UPDATE files SET status='deleted' WHERE path=?", (row["path"],))

    resolved = db.resolve_calls()

    scip_stats = None
    if scip not in (None, "none", "off"):
        from . import scip as scip_mod

        idx = None if scip in ("auto", True) else Path(scip)
        scip_stats = scip_mod.run(db, root, index_path=idx)
        if scip_stats and progress:
            progress({"file": scip_stats["index"],
                      "scip_edges": scip_stats["edges"]})

    lsp_stats = None
    if lsp:
        from . import lsp as lsp_mod

        db.recompute_degrees()
        lsp_stats = lsp_mod.resolve(db, root, progress=progress)

    sem_stats = None
    backend = None
    want_skill = semantic == "skill"
    if semantic not in (None, "none", "skill"):
        from .llm import detect_backend, make_backend

        backend = detect_backend() if semantic == "auto" else make_backend(semantic)
    if backend is not None:
        from .semantic import run as run_semantic

        sem_stats = run_semantic(
            db, root, backend, force=semantic_force or force,
            progress=progress if progress else None,
        )

    db.reindex_fts()  # single clean rebuild after all per-file replacements
    db.recompute_degrees()

    from .cluster import cluster
    from .analyze import analyze

    cluster(db)
    if backend is not None:
        from .semantic import name_communities

        try:
            r = name_communities(db, backend,
                                 progress=progress if progress else None)
            if sem_stats is not None:
                sem_stats["communities_named"] = r["named"]
        except Exception:  # naming is best-effort; keep the heuristic labels
            pass
    analyze(db)

    from .render.graph_json import write_graph_json
    from .render.html import write_html
    from .render.report import write_report

    write_graph_json(db, out_dir(root))
    write_report(db, out_dir(root))
    write_html(db, out_dir(root))

    req_stats = None
    if want_skill:
        from .semantic import write_request

        req_stats = write_request(db, root, force=semantic_force or force)

    stats = db.stats()
    stats["semantic_request"] = req_stats
    stats["files_indexed"] = len(results)
    stats["skipped_sensitive"] = len(det.skipped_sensitive)
    stats["resolved_xfile_edges"] = resolved
    stats["scip"] = scip_stats
    stats["lsp"] = lsp_stats
    stats["backup"] = backed_up
    stats["semantic"] = sem_stats
    stats["semantic_backend"] = backend.name if backend else None
    db.close()
    return stats


def update(root: Path | str, *, progress=None) -> dict:
    """Incremental: re-index only content-changed / new / deleted code files
    (no LLM), then re-resolve, re-cluster, re-render."""
    return extract(root, force=False, semantic="none", progress=progress)


def check_update(root: Path | str) -> dict:
    """Report staleness without touching the graph: git HEAD drift plus the
    per-file content diff between the walk and the ``files`` table."""
    root = Path(root).resolve()
    db = Db(db_path(root), create=False)
    prev = {r["path"]: r for r in db.all_files(status="present")}
    built = db.get_meta("built_at_commit")
    db.close()

    det = detect(root)
    # `extract` indexes code + docs; ingested sources under the output dir are
    # never walked and must not be reported as deleted.
    walked = list(det.code) + [f for f in det.files
                               if f.file_type in ("document", "paper")]
    now = {f.rel: f for f in walked}
    ingested_prefix = out_dir(root).name + "/"

    changed = sorted(r for r in now if r in prev
                     and prev[r]["content_sha256"] != now[r].content_sha256)
    added = sorted(r for r in now if r not in prev)
    deleted = sorted(r for r in prev
                     if r not in now and not r.startswith(ingested_prefix))

    head = _git_head(root)
    head_moved = bool(built and head and built[:12] != head[:12])
    stale = head_moved or bool(changed or added or deleted)
    return {
        "stale": stale, "head_moved": head_moved,
        "built_at_commit": built, "head": head,
        "changed": changed, "added": added, "deleted": deleted,
    }
