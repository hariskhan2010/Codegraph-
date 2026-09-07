"""``codegraph merge-graphs`` — combine several project graphs into one.

Each source keeps its identity through a ``{name}:`` slug prefix and a
``{name}/`` path prefix, so nodes never collide and every row stays traceable to
its origin repo. The merged DB is re-clustered and re-analysed so cross-repo
communities and god nodes surface.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__
from .config import db_path, out_dir
from .db import Db


def _name_for(src: Path, given: str | None) -> str:
    if given:
        return given
    p = src if src.suffix != ".db" else src.parent.parent
    return p.resolve().name or "repo"


def merge(sources: list[Path | str], out_root: Path | str,
          names: list[str] | None = None) -> dict:
    names = names or [None] * len(sources)  # type: ignore[list-item]
    out_root = Path(out_root).resolve()
    out_dir(out_root).mkdir(parents=True, exist_ok=True)
    dst_path = db_path(out_root)
    if dst_path.exists():
        dst_path.unlink()
    dst = Db(dst_path)
    dst.set_meta("root", out_root.as_posix())
    dst.set_meta("codegraph_version", __version__)
    dst.set_meta("merged_from", ",".join(
        _name_for(Path(s), n) for s, n in zip(sources, names)))

    per_source = []
    with dst.tx() as c:
        for src, given in zip(sources, names):
            src = Path(src)
            spath = src if src.suffix == ".db" else db_path(src)
            if not spath.exists():
                raise FileNotFoundError(f"no graph at {spath}")
            tag = _name_for(src, given)
            sdb = Db(spath, create=False)
            idmap: dict[int, int] = {}
            for r in sdb.nodes():
                extra = r["extra"]
                cur = c.execute(
                    "INSERT INTO nodes(slug,label,norm_label,file_type,source_file,"
                    "source_location,definition_file,definition_location,kind,origin,"
                    "rationale,extra) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        f"{tag}:{r['slug']}" if r["slug"] else "",
                        r["label"], r["norm_label"], r["file_type"],
                        f"{tag}/{r['source_file']}" if r["source_file"] else None,
                        r["source_location"], r["definition_file"],
                        r["definition_location"], r["kind"], r["origin"],
                        r["rationale"], extra,
                    ),
                )
                idmap[int(r["id"])] = int(cur.lastrowid)
            n_e = 0
            for e in sdb.edges():
                s, d = idmap.get(int(e["src"])), idmap.get(int(e["dst"]))
                if s is None or d is None:
                    continue
                c.execute(
                    "INSERT OR IGNORE INTO edges(src,dst,relation,confidence,"
                    "confidence_score,context,source_file,source_location,weight,"
                    "evidence,deferred,type_only) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        s, d, e["relation"], e["confidence"], e["confidence_score"],
                        e["context"],
                        f"{tag}/{e['source_file']}" if e["source_file"] else None,
                        e["source_location"], e["weight"], e["evidence"],
                        e["deferred"], e["type_only"],
                    ),
                )
                n_e += 1
            per_source.append({"name": tag, "nodes": len(idmap), "edges": n_e})
            sdb.close()

    dst.reindex_fts()
    dst.recompute_degrees()

    from .analyze import analyze
    from .cluster import cluster
    from .render.graph_json import write_graph_json
    from .render.html import write_html
    from .render.report import write_report

    cluster(dst)
    analyze(dst)
    write_graph_json(dst, out_dir(out_root))
    write_report(dst, out_dir(out_root))
    write_html(dst, out_dir(out_root))
    st = dst.stats()
    dst.close()
    return {"sources": per_source, **st, "out": str(out_dir(out_root))}
