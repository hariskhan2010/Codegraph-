"""Generic tree-sitter walker: one source file -> nodes + edges + unresolved refs.

The walker is language-agnostic; every language-specific fact comes from a
:class:`~codegraph.extract.langs.LangConfig` (query strings + small ``supers`` /
``import_names`` callables). Structure it can prove from the AST is emitted as
edges immediately (``contains``, ``method``, same-file ``calls``); everything
that needs the whole corpus (cross-file calls, inheritance, imports) is emitted
as a ``raw_ref`` for :meth:`codegraph.db.Db.resolve_calls`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tree_sitter import Node, QueryCursor

from .. import ids
from .langs import LANGS, compiled_query, get_parser


@dataclass
class FileResult:
    rel: str
    lang: str
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    raw_refs: list[dict] = field(default_factory=list)
    error: str | None = None


@dataclass
class _Def:
    node: Node
    name: str
    is_class: bool
    start: int
    end: int
    line: int
    slug: str = ""
    parent: "_Def | None" = None


def _loc(node: Node) -> str:
    a = node.start_point[0] + 1
    b = node.end_point[0] + 1
    return f"L{a}" if a == b else f"L{a}-L{b}"


_HTTP_VERBS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


def _route_prefix(rel: str) -> str | None:
    """Turn a framework route file into its URL-ish path so identically-named
    handlers (`POST` in every Next.js `route.ts`) get distinct labels.

    ``src/app/api/cart/route.ts``            -> ``api/cart``
    ``app/(shop)/products/[id]/route.ts``    -> ``products/[id]``   (route group dropped)
    ``pages/api/checkout.ts``                -> ``api/checkout``
    ``src/routes/users.$id.tsx``             -> ``users/:id``  (Remix-ish, best effort)
    """
    parts = rel.replace("\\", "/").split("/")
    stem = parts[-1].rsplit(".", 1)[0]
    lower = [p.lower() for p in parts]

    # Next.js App Router: .../app/**/route.{ts,js} (or page/layout)
    if stem in ("route", "page", "layout", "default") and "app" in lower:
        i = lower.index("app")
        segs = [p for p in parts[i + 1 : -1]
                if not (p.startswith("(") and p.endswith(")"))]
        path = "/".join(segs)
        return path or "/"

    # Next.js / Nuxt Pages Router: .../pages/**/*.{ts,js,vue}
    if "pages" in lower:
        i = lower.index("pages")
        segs = list(parts[i + 1 : -1])
        if stem != "index":
            segs.append(stem)
        path = "/".join(segs)
        return path or "/"

    return None


def _matches(lang: str, which: str, root: Node) -> list[dict]:
    q = compiled_query(lang, which)
    if q is None:
        return []
    try:
        return [caps for _, caps in QueryCursor(q).matches(root)]
    except Exception:
        return []


def extract_file(rel: str, src: bytes, lang: str) -> FileResult:
    res = FileResult(rel=rel, lang=lang)
    parser = get_parser(lang)
    cfg = LANGS.get(lang)
    if parser is None or cfg is None:
        res.error = f"no parser for {lang}"
        return res
    try:
        tree = parser.parse(src)
    except Exception as e:  # pragma: no cover - defensive
        res.error = f"parse error: {e}"
        return res
    root = tree.root_node

    file_slug = ids.file_slug(rel)
    res.nodes.append({
        "slug": file_slug,
        "label": rel.rsplit("/", 1)[-1],
        "kind": "file",
        "file_type": "code",
        "source_location": "L1",
    })

    # ---- definitions -------------------------------------------------------
    defs: list[_Def] = []
    for caps in _matches(lang, "functions", root):
        fn = (caps.get("fn") or [None])[0]
        nm = (caps.get("name") or [None])[0]
        if fn is None or nm is None:
            continue
        defs.append(_Def(fn, nm.text.decode("utf-8", "replace"), False,
                         fn.start_byte, fn.end_byte, fn.start_point[0] + 1))
    for caps in _matches(lang, "classes", root):
        cl = (caps.get("cls") or [None])[0]
        nm = (caps.get("name") or [None])[0]
        if cl is None or nm is None:
            continue
        defs.append(_Def(cl, nm.text.decode("utf-8", "replace"), True,
                         cl.start_byte, cl.end_byte, cl.start_point[0] + 1))

    # dedupe: overlapping grammars / multiple query patterns can capture the same
    # span twice (e.g. Swift `class_declaration` with and without a `name:` field).
    _seen_spans: set[tuple[int, int]] = set()
    _uniq: list[_Def] = []
    for d in sorted(defs, key=lambda d: (d.start, -d.end, not d.is_class)):
        if (d.start, d.end) in _seen_spans:
            continue
        _seen_spans.add((d.start, d.end))
        _uniq.append(d)
    defs = _uniq

    defs.sort(key=lambda d: (d.start, -d.end))

    def enclosing(pos_start: int, pos_end: int, exclude: _Def | None = None) -> _Def | None:
        best: _Def | None = None
        for d in defs:
            if d is exclude:
                continue
            if d.start <= pos_start and d.end >= pos_end and (d.start, d.end) != (pos_start, pos_end):
                if best is None or d.start > best.start or (d.start == best.start and d.end < best.end):
                    best = d
        return best

    for d in defs:
        d.parent = enclosing(d.start, d.end, exclude=d)

    # slugs from the name chain
    for d in defs:
        chain: list[str] = []
        cur: _Def | None = d
        while cur is not None:
            chain.append(cur.name)
            cur = cur.parent
        d.slug = ids.symbol_slug(rel, *reversed(chain))

    seen_slugs: set[str] = {file_slug}
    for d in defs:
        if d.slug in seen_slugs:
            d.slug = ids.make_slug(d.slug, f"l{d.line}")
        seen_slugs.add(d.slug)

    route = _route_prefix(rel)
    same_file_defs: dict[str, _Def] = {}
    for d in defs:
        parent_is_class = d.parent is not None and d.parent.is_class
        kind = "class" if d.is_class else ("method" if parent_is_class else "function")
        if d.is_class:
            label = d.name
        elif parent_is_class:
            label = f".{d.name}()"
        elif route and d.parent is None and d.name.upper() in _HTTP_VERBS:
            # Next.js / framework route handler: disambiguate `POST` by its path
            label = f"{d.name.upper()} {route}"
            kind = "route"
        elif route and d.parent is None and d.name in ("default", "handler", "Page"):
            label = f"{route} ({d.name})"
            kind = "route"
        else:
            label = f"{d.name}()"
        res.nodes.append({
            "slug": d.slug, "label": label, "kind": kind,
            "file_type": "code", "source_location": _loc(d.node),
        })
        same_file_defs.setdefault(d.name, d)

        parent_slug = d.parent.slug if d.parent else file_slug
        rel_edge = "method" if (d.parent and d.parent.is_class) else "contains"
        res.edges.append({
            "src_slug": parent_slug, "dst_slug": d.slug, "relation": rel_edge,
            "confidence": "EXTRACTED", "confidence_score": 1.0,
            "context": "contains", "source_location": _loc(d.node),
            "evidence": "same-file",
        })

        # inheritance
        if d.is_class and cfg.supers:
            for base in cfg.supers(d.node):
                local = same_file_defs.get(base)
                if local and local.is_class:
                    res.edges.append({
                        "src_slug": d.slug, "dst_slug": local.slug,
                        "relation": "inherits", "confidence": "EXTRACTED",
                        "confidence_score": 1.0, "context": "type",
                        "source_location": _loc(d.node), "evidence": "same-file",
                    })
                else:
                    res.raw_refs.append({
                        "caller_slug": d.slug, "callee": base, "relation": "inherits",
                        "source_location": _loc(d.node), "lang": lang,
                    })

    # ---- imports (evidence for call resolution) --------------------------
    imported: set[str] = set()
    for caps in _matches(lang, "imports", root):
        imp = (caps.get("import") or [None])[0]
        if imp is None:
            continue
        stmt = imp
        while stmt.parent and stmt.type not in (
            "import_statement", "import_from_statement", "import_declaration",
            "using_directive", "use_declaration", "preproc_include", "call",
            "call_expression", "import_header", "namespace_use_declaration",
            "command", "using_statement", "alias", "identifier",
        ):
            stmt = stmt.parent
        names = cfg.import_names(stmt, src) if cfg.import_names else []
        for nm in names:
            imported.add(nm)
            res.raw_refs.append({
                "caller_slug": file_slug, "callee": nm, "relation": "imports",
                "source_location": _loc(stmt), "lang": lang, "import_evidence": True,
            })

    # ---- calls ----------------------------------------------------------
    for caps in _matches(lang, "calls", root):
        callee = (caps.get("callee") or [None])[0]
        if callee is None:
            continue
        name = callee.text.decode("utf-8", "replace")
        if not name or name in _NOISE_CALLEES:
            continue
        enc = enclosing(callee.start_byte, callee.start_byte + 1)
        caller_slug = enc.slug if enc else file_slug
        local = same_file_defs.get(name)
        if local and local.slug != caller_slug:
            res.edges.append({
                "src_slug": caller_slug, "dst_slug": local.slug, "relation": "calls",
                "confidence": "EXTRACTED", "confidence_score": 1.0, "context": "call",
                "source_location": f"L{callee.start_point[0] + 1}", "evidence": "same-file",
            })
        elif not local:
            res.raw_refs.append({
                "caller_slug": caller_slug, "callee": name, "relation": "calls",
                "source_location": f"L{callee.start_point[0] + 1}",
                "import_evidence": name in imported, "lang": lang,
            })

    return res


_NOISE_CALLEES = {
    "print", "len", "str", "int", "float", "bool", "list", "dict", "set", "tuple",
    "range", "type", "super", "isinstance", "hasattr", "getattr", "setattr",
    "console", "require", "String", "Number", "Boolean", "Array", "Object",
    "new", "make", "append", "push", "map", "filter", "forEach", "then",
    # shell builtins
    "echo", "source", "cd", "export", "local", "eval", "test", "read", "printf",
    "exit", "shift", "unset", "declare", "readonly", "trap", "wait",
    # common stdlib-ish
    "println", "toString", "valueOf", "format", "sprintf", "paste",
    # elixir / metaprogramming keywords that are `call` targets, not real calls
    "def", "defp", "defmodule", "defprotocol", "defimpl", "defmacro", "defstruct",
    "import", "alias", "require", "use", "quote", "unquote",
    # r / julia / powershell
    "library", "c", "return", "Write-Host", "Write-Output", "param",
}
