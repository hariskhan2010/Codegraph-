"""``codegraph`` command-line interface.

Commands: extract / update / watch / check-update / query / context / explain /
path / affected / god-nodes / stats / diagnose / embed / merge-graphs / global /
add / clone / install (multi-platform) / merge-driver / prs / export / serve / save-result /
reflect. Command names and shapes match graphify so the ``/codegraph`` agent
skill and existing configs carry over.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import db_path, out_dir


def _need_db(root: Path):
    p = db_path(root)
    if not p.exists():
        sys.exit(f"no graph at {p} — run `codegraph extract {root}` first")
    from .db import Db

    return Db(p, create=False)


def cmd_extract(args) -> int:
    from .pipeline import extract

    def prog(r):
        if "nodes" in r:
            mark = "!" if r.get("error") else "+"
            print(f"  [{mark}] {r['file']}  ({r['nodes']}n {r['edges']}e)"
                  + (f"  {r['error']}" if r.get("error") else ""))
        elif "annotated" in r:
            print(f"  [~] {r['file']}  (+{r['annotated']} rationale, "
                  f"+{r['amb_edges']} ambiguous)")
        elif r.get("error"):
            print(f"  [!] {r['file']}  semantic: {r['error']}")

    semantic = "none" if args.no_semantic else (args.semantic or "auto")
    scip = "none" if args.no_scip else (args.scip or "auto")
    st = extract(args.path, force=args.force, semantic=semantic,
                 docs=not args.no_docs, scip=scip, lsp=args.lsp,
                 progress=prog if args.verbose else None)
    print(f"\n{st['nodes']} nodes · {st['edges']} edges · {st['communities']} communities "
          f"· {st['files']} files")
    print(f"resolved {st['resolved_xfile_edges']} cross-file edges · "
          f"{st['skipped_sensitive']} sensitive files skipped")
    if st.get("scip"):
        print(f"scip tier: +{st['scip']['edges']} scope-aware edges from "
              f"{st['scip']['documents']} indexed docs")
    if st.get("lsp"):
        L = st["lsp"]
        print(f"lsp tier ({', '.join(L['families']) or 'no server found'}): "
              f"+{L['edges']} edges from {L['defs_probed']} probed defs"
              + (f"  [{'; '.join(L['errors'])}]" if L["errors"] else ""))
    if st.get("semantic"):
        s = st["semantic"]
        print(f"semantic ({st['semantic_backend']}): {s['files']} files · "
              f"{s['annotated']} rationale · {s['amb_edges']} ambiguous edges · "
              f"{s['dropped']} dropped · {s['failed']} failed")
    elif not args.no_semantic:
        print("semantic: no LLM backend available (set an API key or install "
              "`claude`) — AST-only graph")
    if st.get("backup"):
        print(f"backed up previous artifacts -> {st['backup']}")
    print(f"-> {out_dir(Path(args.path).resolve())}")
    return 0


def cmd_update(args) -> int:
    from .pipeline import update

    st = update(args.path)
    n = st.get("files_indexed", 0)
    print(f"re-indexed {n} changed file{'s' if n != 1 else ''}"
          if n else "no files changed")
    print(f"{st['nodes']} nodes · {st['edges']} edges · {st['communities']} communities")
    if st.get("backup"):
        print(f"backed up previous artifacts -> {st['backup']}")
    print(f"-> {out_dir(Path(args.path).resolve())}")
    return 0


def cmd_watch(args) -> int:
    import time

    from .pipeline import update

    root = Path(args.path).resolve()
    print(f"watching {root} (Ctrl-C to stop)")
    last: dict[str, float] = {}
    try:
        while True:
            changed = False
            for p in root.rglob("*"):
                if p.is_file() and out_dir(root) not in p.parents:
                    m = p.stat().st_mtime
                    if last.get(str(p)) != m:
                        last[str(p)] = m
                        changed = True
            if changed and last:
                st = update(root)
                print(f"  rebuilt: {st['nodes']}n {st['edges']}e")
            time.sleep(2)
    except KeyboardInterrupt:
        return 0


def cmd_query(args) -> int:
    from .query import query

    if getattr(args, "all", False):
        return _query_all(args)
    db = _need_db(Path(args.path))
    print(query(db, args.question, depth=args.depth, budget=args.budget))
    db.close()
    return 0


def _query_all(args) -> int:
    from .db import Db
    from .query import query
    from .registry import entries

    reg = entries()
    if not reg:
        sys.exit("no registered graphs — `codegraph global add <name> <path>`")
    per_budget = max(400, args.budget // max(1, len(reg)))
    for name in sorted(reg):
        p = db_path(Path(reg[name]["path"]))
        if not p.exists():
            print(f"### {name} — graph missing at {p}\n")
            continue
        db = Db(p, create=False)
        print(f"### {name}  ({reg[name]['path']})")
        print(query(db, args.question, depth=args.depth, budget=per_budget))
        print()
        db.close()
    return 0


def cmd_check_update(args) -> int:
    from .pipeline import check_update

    r = check_update(Path(args.path))
    if r["head_moved"]:
        print(f"git HEAD moved: built {r['built_at_commit'][:12]} -> now {r['head'][:12]}")
    for tag, key in (("changed", "changed"), ("new", "added"), ("deleted", "deleted")):
        if r[key]:
            print(f"{tag} ({len(r[key])}): {', '.join(r[key][:12])}"
                  + (" …" if len(r[key]) > 12 else ""))
    if not r["stale"]:
        print("up to date")
    elif not args.quiet:
        print("\nrun `codegraph update` (or `extract` if you want the LLM pass)")
    return 1 if (r["stale"] and args.exit_code) else 0


def cmd_merge(args) -> int:
    from .merge import merge

    names = args.names.split(",") if args.names else None
    if names and len(names) != len(args.sources):
        sys.exit("--names must have one entry per source")
    r = merge(args.sources, args.out, names)
    for s in r["sources"]:
        print(f"  {s['name']}: {s['nodes']} nodes, {s['edges']} edges")
    print(f"\nmerged -> {r['nodes']} nodes · {r['edges']} edges · "
          f"{r['communities']} communities\n-> {r['out']}")
    return 0


def cmd_global(args) -> int:
    from . import registry

    if args.action == "list":
        reg = registry.entries()
        if not reg:
            print("no registered graphs")
        for name in sorted(reg):
            print(f"  {name:20} {reg[name]['path']}")
        return 0
    if args.action == "add":
        if not args.name or not args.target:
            sys.exit("usage: codegraph global add <name> <path>")
        registry.add(args.name, args.target)
        print(f"registered {args.name}")
        return 0
    if args.action == "remove":
        ok = registry.remove(args.name or "")
        print("removed" if ok else f"no such entry: {args.name}")
        return 0 if ok else 1
    return 2


def cmd_explain(args) -> int:
    from .query import explain

    db = _need_db(Path(args.path))
    print(explain(db, args.node))
    db.close()
    return 0


def cmd_path(args) -> int:
    from .query import shortest_path

    db = _need_db(Path(args.path))
    print(shortest_path(db, args.source, args.target))
    db.close()
    return 0


def cmd_affected(args) -> int:
    from .query import affected

    db = _need_db(Path(args.path))
    print(affected(db, args.node, depth=args.depth))
    db.close()
    return 0


def cmd_context(args) -> int:
    from .context import context

    db = _need_db(Path(args.path))
    print(context(db, args.node, callers=args.callers, callees=args.callees,
                  body_lines=args.body_lines))
    db.close()
    return 0


def cmd_god_nodes(args) -> int:
    import json

    from .analyze import god_nodes

    db = _need_db(Path(args.path))
    gs = god_nodes(db, args.top)
    if args.json:
        print(json.dumps(gs, indent=2))
    else:
        for i, g in enumerate(gs, 1):
            print(f"{i:2}. {g['label']}  ({g['degree']} edges)")
    db.close()
    return 0


def cmd_stats(args) -> int:
    db = _need_db(Path(args.path))
    st = db.stats()
    print(f"nodes        {st['nodes']}")
    print(f"edges        {st['edges']}")
    print(f"communities  {st['communities']}")
    print(f"files        {st['files']}")
    print(f"confidence   {st['confidence']}")
    db.close()
    return 0


def cmd_embed(args) -> int:
    from .embed import build
    from .llm import detect_embed_backend, make_embed_backend

    db = _need_db(Path(args.path))
    backend = (detect_embed_backend() if args.backend in (None, "auto")
               else make_embed_backend(args.backend))
    if backend is None:
        db.close()
        sys.exit("no embeddings backend available — set OPENAI_API_KEY / GEMINI_API_KEY, "
                 "run ollama, or pass --backend hash for the (lexical-only) fallback")
    r = build(db, backend, progress=lambda x: None)
    db.close()
    print(f"embedded {r['stored']}/{r['nodes']} nodes via {r['model']} "
          f"({r['cache_hits']} cached, {r['api_calls']} new)")
    return 0


def cmd_diagnose(args) -> int:
    import json

    from .diagnose import diagnose, format_report

    db = _need_db(Path(args.path))
    d = diagnose(db)
    db.close()
    print(json.dumps(d, indent=2) if args.json else format_report(d))
    return 1 if d["failed"] and args.strict else 0


def cmd_export(args) -> int:
    from .export import TARGETS

    db = _need_db(Path(args.path))
    root = Path(args.path).resolve()
    out = out_dir(root)
    native = {"json", "report", "html"}
    tgt = args.target
    want = (native | set(TARGETS)) if tgt == "all" else {tgt}

    if "json" in want:
        from .render.graph_json import write_graph_json

        print(write_graph_json(db, out))
    if "report" in want:
        from .render.report import write_report

        print(write_report(db, out))
    if "html" in want:
        from .render.html import write_html

        print(write_html(db, out))
    for t in TARGETS:
        if t in want:
            from .export import export

            for p in export(db, t, root):
                print(p)
    if getattr(args, "run", False):
        from .export import load_cypher

        try:
            r = load_cypher(db, uri=args.uri, user=args.user, password=args.password)
            print(f"loaded {r['statements']} statements into {r['uri']} via {r['driver']}")
        except RuntimeError as e:
            db.close()
            sys.exit(str(e))
    db.close()
    return 0


def cmd_add(args) -> int:
    from .ingest import IngestError, add

    try:
        r = add(args.path, args.source, title=args.title)
    except IngestError as e:
        sys.exit(str(e))
    print(f"added \"{r['title']}\" — {r['sections']} sections, {r['chars']} chars")
    print(f"  {r['path']}")
    print(f"  query it: codegraph query \"...\" {args.path}")
    return 0


def cmd_clone(args) -> int:
    import shutil

    from .db import Db
    from .render.graph_json import write_graph_json
    from .render.html import write_html
    from .render.report import write_report

    src = Path(args.source)
    sp = src if src.suffix == ".db" else db_path(src)
    if not sp.exists():
        sys.exit(f"no graph at {sp}")
    dst = Path(args.dest).resolve()
    dp = db_path(dst)
    dp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(sp, dp)
    db = Db(dp, create=False)
    db.set_meta("root", dst.as_posix())
    db.conn.commit()
    write_graph_json(db, out_dir(dst))
    write_report(db, out_dir(dst))
    write_html(db, out_dir(dst))
    db.close()
    print(f"cloned -> {dp}")
    return 0


def cmd_install(args) -> int:
    import sys as _sys

    from .installers import install as install_mcp

    target = Path(args.path).resolve()
    if not args.git_only:
        try:
            r = install_mcp(args.platform, target)
        except ValueError as e:
            sys.exit(str(e))
        print(f"wrote {r['path']}  ({r['platform']}, {r['scope']} scope)")
        if r["platform"] == "claude":
            print("Claude Code picks this up on next start (or: "
                  f"claude mcp add codegraph -- {_sys.executable} "
                  f"-m codegraph serve {target})")

    if args.git or args.git_only:
        from .gitmerge import install as install_merge_driver

        for note in install_merge_driver(target):
            print(f"  {note}")
    return 0


def cmd_merge_driver(args) -> int:
    from .gitmerge import merge_driver

    return merge_driver(args.base, args.ours, args.theirs, args.path_name)


def cmd_prs(args) -> int:
    import json

    from . import prs

    try:
        if args.number is not None:
            db = _need_db(Path(args.path))
            r = prs.pr_impact(db, args.number, depth=args.depth)
            db.close()
            if args.json:
                print(json.dumps(r, indent=2))
            else:
                print(f"PR #{r['number']}  {r['title']}")
                print(f"  {r['changed_nodes']} nodes changed across "
                      f"{len(r['changed_files'])} files")
                print(f"  blast radius: {r['blast_radius']} downstream files, "
                      f"{r['affected_nodes']} nodes")
                for f in r["affected_files"][:20]:
                    print(f"    {f}")
            return 0
        db = _need_db(Path(args.path))
        rows = prs.triage(db, args.limit, depth=args.depth)
        db.close()
        if args.json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("no open PRs (or none touch graphed files)")
        for p in rows:
            print(f"#{p['number']:<5} radius {p['blast_radius']:<4} "
                  f"{p['title'][:60]}")
        return 0
    except prs.GhUnavailable as e:
        sys.exit(f"PR tools need the GitHub CLI: {e}")


def cmd_serve(args) -> int:
    if args.http:
        from .serve import serve_http

        serve_http(Path(args.path), host=args.host, port=args.port)
    else:
        from .serve import serve

        serve(Path(args.path))
    return 0


def cmd_save_result(args) -> int:
    from .reflect import save_result

    db = _need_db(Path(args.path))
    answer = args.answer
    if args.answer_file:
        answer = Path(args.answer_file).read_text(encoding="utf-8")
    save_result(db, args.question, answer or "", outcome=args.outcome,
                correction=args.correction, source_nodes=args.nodes or [])
    db.close()
    print("recorded")
    return 0


def cmd_reflect(args) -> int:
    from .reflect import reflect

    db = _need_db(Path(args.path))
    r = reflect(db, half_life_days=args.half_life, min_corroboration=args.min_corroboration)
    print(f"{r['total']} recorded results · {len(r['preferred'])} preferred sources "
          f"· {len(r['dead_ends'])} dead ends")
    for p in r["preferred"]:
        print(f"  + {p['node']}  ({p['pos']}x useful)")
    for d in r["dead_ends"]:
        if "node" in d:
            print(f"  - {d['node']}  (dead end)")
    from .render.report import write_report

    write_report(db, out_dir(Path(args.path).resolve()))
    db.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codegraph", description=__doc__)
    p.add_argument("-V", "--version", action="version", version=f"codegraph {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def with_path(sp):
        sp.add_argument("path", nargs="?", default=".", help="project root")
        return sp

    e = with_path(sub.add_parser("extract", help="full build"))
    e.add_argument("--force", action="store_true")
    e.add_argument("--semantic", metavar="BACKEND",
                   help="LLM backend for the annotation pass "
                        "(anthropic|openai|gemini|ollama|claude-cli); default: auto-detect")
    e.add_argument("--no-semantic", action="store_true", help="skip the LLM pass")
    e.add_argument("--no-docs", action="store_true",
                   help="skip Markdown / reST / AsciiDoc section indexing")
    e.add_argument("--scip", metavar="PATH",
                   help="use a specific SCIP index (default: auto-detect *.scip)")
    e.add_argument("--no-scip", action="store_true",
                   help="skip the SCIP indexer tier")
    e.add_argument("--lsp", action="store_true",
                   help="resolve calls with a running language server (precise, slow; "
                        "needs pylsp / gopls / rust-analyzer / … on PATH)")
    e.add_argument("-v", "--verbose", action="store_true")
    e.set_defaults(func=cmd_extract)

    with_path(sub.add_parser("update", help="incremental rebuild (no LLM)")).set_defaults(func=cmd_update)
    with_path(sub.add_parser("watch", help="rebuild on file change")).set_defaults(func=cmd_watch)

    cu = with_path(sub.add_parser("check-update", help="report staleness without rebuilding"))
    cu.add_argument("--exit-code", action="store_true", help="exit 1 when stale")
    cu.add_argument("-q", "--quiet", action="store_true")
    cu.set_defaults(func=cmd_check_update)

    mg = sub.add_parser("merge-graphs", help="combine several project graphs into one")
    mg.add_argument("sources", nargs="+", help="project roots or codegraph.db paths")
    mg.add_argument("-o", "--out", required=True, help="output project root")
    mg.add_argument("--names", help="comma-separated tag per source (default: dir name)")
    mg.set_defaults(func=cmd_merge)

    gl = sub.add_parser("global", help="user-level graph registry (cross-repo query)")
    gl.add_argument("action", choices=["list", "add", "remove"])
    gl.add_argument("name", nargs="?")
    gl.add_argument("target", nargs="?", help="project path (for add)")
    gl.set_defaults(func=cmd_global)

    q = sub.add_parser("query", help="ask the graph")
    q.add_argument("question")
    q.add_argument("path", nargs="?", default=".")
    q.add_argument("--depth", type=int, default=2)
    q.add_argument("--budget", type=int, default=2000)
    q.add_argument("--all", action="store_true",
                   help="fan the question across every registered graph")
    q.set_defaults(func=cmd_query)

    x = sub.add_parser("explain", help="describe one node and its connections")
    x.add_argument("node")
    x.add_argument("path", nargs="?", default=".")
    x.set_defaults(func=cmd_explain)

    pa = sub.add_parser("path", help="shortest path between two nodes")
    pa.add_argument("source")
    pa.add_argument("target")
    pa.add_argument("path", nargs="?", default=".")
    pa.set_defaults(func=cmd_path)

    af = sub.add_parser("affected", help="what breaks if you change this symbol")
    af.add_argument("node")
    af.add_argument("path", nargs="?", default=".")
    af.add_argument("--depth", type=int, default=3)
    af.set_defaults(func=cmd_affected)

    cx = sub.add_parser("context", help="emit the code an agent needs for a symbol "
                        "(body + caller/callee signatures)")
    cx.add_argument("node")
    cx.add_argument("path", nargs="?", default=".")
    cx.add_argument("--callers", type=int, default=8)
    cx.add_argument("--callees", type=int, default=10)
    cx.add_argument("--body-lines", type=int, default=60)
    cx.set_defaults(func=cmd_context)

    g = with_path(sub.add_parser("god-nodes", help="most connected nodes"))
    g.add_argument("--top", type=int, default=15)
    g.add_argument("--json", action="store_true")
    g.set_defaults(func=cmd_god_nodes)

    with_path(sub.add_parser("stats", help="graph size summary")).set_defaults(func=cmd_stats)

    em = with_path(sub.add_parser("embed", help="compute node embeddings for "
                                  "synonym retrieval (opt-in; needs a model)"))
    em.add_argument("--backend", metavar="NAME",
                    help="openai|gemini|ollama|hash; default: auto-detect")
    em.set_defaults(func=cmd_embed)

    dg = with_path(sub.add_parser("diagnose", help="graph-health check"))
    dg.add_argument("--json", action="store_true")
    dg.add_argument("--strict", action="store_true", help="exit 1 if any check warns")
    dg.set_defaults(func=cmd_diagnose)

    from .export import TARGETS as _EXPORT_TARGETS

    ex = sub.add_parser("export", help="re-render an output from the db")
    ex.add_argument("target",
                    choices=["json", "report", "html", "all", *_EXPORT_TARGETS],
                    default="all", nargs="?")
    ex.add_argument("path", nargs="?", default=".", help="project root")
    ex.add_argument("--run", action="store_true",
                    help="with target=cypher: push straight into a live Neo4j/FalkorDB")
    ex.add_argument("--uri", help="bolt URI (default: $NEO4J_URI or bolt://localhost:7687)")
    ex.add_argument("--user", help="graph DB user (default: $NEO4J_USER or neo4j)")
    ex.add_argument("--password", help="graph DB password (default: $NEO4J_PASSWORD)")
    ex.set_defaults(func=cmd_export)

    ad = sub.add_parser("add", help="ingest an external doc into the graph "
                        "(URL / arXiv / notion: / confluence: / local file)")
    ad.add_argument("source", help="URL, arxiv.org/abs/ID, notion:<id>, "
                    "confluence:<id>, or a local .md/.pdf/.docx/.mp4 …")
    ad.add_argument("path", nargs="?", default=".", help="project root")
    ad.add_argument("--title")
    ad.set_defaults(func=cmd_add)

    cl = sub.add_parser("clone", help="copy a graph to a new location")
    cl.add_argument("source", help="project root or codegraph.db")
    cl.add_argument("dest", help="destination project root")
    cl.set_defaults(func=cmd_clone)

    from .installers import PLATFORMS as _PLATFORMS

    ins = with_path(sub.add_parser("install", help="register the MCP server with an "
                                   "agent platform (+ optional git merge driver)"))
    ins.add_argument("--platform", default="claude", choices=sorted(_PLATFORMS),
                     help="target agent (default: claude)")
    ins.add_argument("--git", action="store_true",
                     help="also install the codegraph-out/ git merge driver")
    ins.add_argument("--git-only", action="store_true",
                     help="only install the git merge driver")
    ins.set_defaults(func=cmd_install)

    md = sub.add_parser("merge-driver", help=argparse.SUPPRESS)  # invoked by git
    md.add_argument("base")
    md.add_argument("ours")
    md.add_argument("theirs")
    md.add_argument("path_name", nargs="?", default="")
    md.set_defaults(func=cmd_merge_driver)

    pr = with_path(sub.add_parser("prs", help="open-PR impact ranked by blast radius (needs `gh`)"))
    pr.add_argument("number", nargs="?", type=int, help="a single PR number")
    pr.add_argument("--limit", type=int, default=30)
    pr.add_argument("--depth", type=int, default=3)
    pr.add_argument("--json", action="store_true")
    pr.set_defaults(func=cmd_prs)

    sv = with_path(sub.add_parser("serve", help="MCP server (stdio, or --http)"))
    sv.add_argument("--http", action="store_true", help="serve over HTTP instead of stdio")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.set_defaults(func=cmd_serve)

    sr = sub.add_parser("save-result", help="record whether a query answer was useful")
    sr.add_argument("question")
    sr.add_argument("path", nargs="?", default=".")
    sr.add_argument("--answer", default="")
    sr.add_argument("--answer-file")
    sr.add_argument("--outcome", choices=["useful", "dead_end", "corrected"])
    sr.add_argument("--correction")
    sr.add_argument("--nodes", nargs="*", help="labels of the nodes that answered it")
    sr.set_defaults(func=cmd_save_result)

    rf = with_path(sub.add_parser("reflect", help="aggregate recorded results into lessons"))
    rf.add_argument("--half-life", type=float, default=30.0)
    rf.add_argument("--min-corroboration", type=int, default=2)
    rf.set_defaults(func=cmd_reflect)

    return p


def _force_utf8_stdio() -> None:
    """Windows consoles default to a legacy code page (cp1252) that cannot encode
    the box-drawing / arrow characters used in reports. Reconfigure rather than
    strip them (graphify issue #19 territory)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    args = build_parser().parse_args(argv)
    return args.func(args)
