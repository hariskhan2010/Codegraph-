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


# tree-sitter node types, across the 20 grammars, that mean "a.b" / "a::b" / "a->b"
# — i.e. the captured callee has a *receiver*. Used to tell `session.execute()`
# apart from a bare `execute()` without touching any per-language query.
_MEMBER_ACCESS_TYPES = frozenset({
    "attribute", "member_expression", "member_access_expression",
    "selector_expression", "field_expression", "field_access",
    "navigation_expression", "navigation_suffix",
    "member_call_expression", "scoped_call_expression", "scoped_identifier",
    "qualified_identifier", "call_expression_statement",
    "dot_index_expression", "method_index_expression",
})
_CALL_TYPES = frozenset({
    "call", "call_expression", "method_invocation", "method_call",
    "function_call", "function_call_expression", "invocation_expression",
    "command", "macro_invocation",
})
# receiver tokens that mean "the current instance" — a call on one of these
# resolves against the caller's own class, which is high precision.
_SELF_NAMES = frozenset({"self", "this", "cls", "me", "super", "_self"})

_BIND_RE = None  # compiled lazily in _class_bindings


def _ident_text(n: Node) -> str | None:
    """Text of an identifier-ish leaf, else None."""
    if n.type in (
        "identifier", "simple_identifier", "field_identifier", "property_identifier",
        "type_identifier", "shorthand_property_identifier", "name", "constant",
        "variable_name", "word",
    ):
        return n.text.decode("utf-8", "replace").lstrip("$@&")
    return None


def _receiver_of(callee: Node) -> tuple[str | None, str, str | None]:
    """Classify the call whose method name is ``callee``.

    Returns ``(receiver_name, kind, receiver_text)`` where kind is one of:
      ``bare``   — ``foo()``            (no receiver)
      ``self``   — ``self.foo()`` / ``this.foo()``
      ``name``   — ``x.foo()``          (receiver is a single identifier ``x``)
      ``ctor``   — ``Foo().bar()``      (receiver is a constructor call)
      ``complex``— ``a.b.foo()`` etc.   (receiver is a non-trivial expression)
    ``receiver_text`` is the raw source of the receiver (``a.b.c``) — used to
    match dotted module aliases; ``None`` for a bare call.
    """
    p = callee.parent
    hops = 0
    while p is not None and hops < 5:
        t = p.type
        if t in _CALL_TYPES:
            # reached the call with no member-access wrapper on the way up
            recv = p.child_by_field_name("receiver") or p.child_by_field_name("object")
            if recv is None:
                return None, "bare", None
            p = recv
            break
        if t in _MEMBER_ACCESS_TYPES:
            recv = None
            for fld in ("object", "receiver", "value", "operand", "argument", "scope"):
                r = p.child_by_field_name(fld)
                if r is not None and r.id != callee.id and callee.start_byte >= r.end_byte:
                    recv = r
                    break
            if recv is None:
                for ch in p.named_children:
                    if ch.id != callee.id and ch.end_byte <= callee.start_byte:
                        recv = ch
                        break
            if recv is None:
                p = p.parent
                hops += 1
                continue
            p = recv
            break
        p = p.parent
        hops += 1
    else:
        return None, "bare", None
    if p is None:
        return None, "bare", None

    # p is now the receiver expression
    text = p.text.decode("utf-8", "replace").strip()
    if len(text) > 60 or "\n" in text:
        text = None
    name = _ident_text(p)
    if name is not None:
        return name, ("self" if name.lower() in _SELF_NAMES else "name"), text
    if p.type in _CALL_TYPES:
        fn = p.child_by_field_name("function") or p.child_by_field_name("name")
        cn = _ident_text(fn) if fn is not None else None
        if cn and cn[:1].isupper():
            return cn, "ctor", cn
        return None, "complex", text
    # a.b.foo() — a dotted path, possibly a module alias
    if text and all(seg.isidentifier() for seg in text.split(".") if seg):
        first = text.split(".")[0]
        if first.lower() in _SELF_NAMES:
            return first, "self_attr", text
        return text.rsplit(".", 1)[0], "dotted", text
    first = _ident_text(p.named_children[0]) if p.named_children else None
    if first and first.lower() in _SELF_NAMES:
        return first, "self_attr", text
    return None, "complex", text


def _class_bindings(text: str) -> dict[str, str]:
    """Best-effort ``local_var -> ClassName`` map from ``x = Foo(...)`` /
    ``x = new Foo(...)`` / ``x := Foo{...}`` inside one function body. High
    precision: the RHS must start with an uppercase identifier."""
    import re

    global _BIND_RE
    if _BIND_RE is None:
        _BIND_RE = re.compile(
            r"(?:^|[;\n{(,])\s*(?:const |let |var |val |my |\$)?"
            r"(\$?[A-Za-z_]\w*)\s*(?::?=|:=)\s*(?:new\s+|await\s+|& )?"
            r"([A-Z]\w*)\s*[({]"
        )
    out: dict[str, str] = {}
    for m in _BIND_RE.finditer(text):
        out.setdefault(m.group(1).lstrip("$"), m.group(2))
    return out


def _resolve_module(module: str, rel: str) -> str:
    """Dotted import module -> posix path fragment, resolving leading-dot
    relative imports against the importing file ``rel``.

    ``pkg.mod`` -> ``pkg/mod``. ``.mod`` in ``a/b/x.py`` -> ``a/b/mod``.
    ``..pkg.mod`` in ``a/b/x.py`` -> ``a/pkg/mod``.
    """
    if not module.startswith("."):
        return module.replace(".", "/")
    dots = len(module) - len(module.lstrip("."))
    tail = module[dots:].replace(".", "/")
    parts = rel.replace("\\", "/").split("/")[:-1]  # package dir of `rel`
    up = dots - 1
    base = parts[: len(parts) - up] if 0 <= up <= len(parts) else []
    return "/".join([*base, tail]).strip("/") if tail else "/".join(base)


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
    # (class name) -> {method name -> _Def}, and the set of class names in this file
    methods_by_class: dict[str, dict[str, _Def]] = {}
    class_names: set[str] = set()
    for d in defs:
        if d.is_class:
            class_names.add(d.name)
        elif d.parent is not None and d.parent.is_class:
            methods_by_class.setdefault(d.parent.name, {}).setdefault(d.name, d)
    for d in defs:
        parent_is_class = d.parent is not None and d.parent.is_class
        kind = "class" if d.is_class else ("method" if parent_is_class else "function")
        extra: dict | None = None
        if d.is_class:
            label = d.name
        elif parent_is_class:
            label = f".{d.name}()"
            extra = {"cls": d.parent.name}
        elif route and d.parent is None and d.name.upper() in _HTTP_VERBS:
            # Next.js / framework route handler: disambiguate `POST` by its path
            label = f"{d.name.upper()} {route}"
            kind = "route"
        elif route and d.parent is None and d.name in ("default", "handler", "Page"):
            label = f"{route} ({d.name})"
            kind = "route"
        else:
            label = f"{d.name}()"
        node = {
            "slug": d.slug, "label": label, "kind": kind,
            "file_type": "code", "source_location": _loc(d.node),
        }
        if extra:
            node["extra"] = extra
        res.nodes.append(node)
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
    # bound name -> module path (posix, relatives resolved), for precise lookup
    sym_import: dict[str, tuple[str, str]] = {}   # name -> (module, symbol)
    mod_alias: dict[str, str] = {}                # alias -> module
    _seen_stmts: set[int] = set()
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
        if cfg.import_specs and stmt.id not in _seen_stmts:
            _seen_stmts.add(stmt.id)
            for bound, module, symbol in cfg.import_specs(stmt, src):
                mod = _resolve_module(module, rel)
                if symbol is None:
                    mod_alias.setdefault(bound, mod)
                else:
                    sym_import.setdefault(bound, (mod, symbol))

    # ---- calls ----------------------------------------------------------
    _bind_cache: dict[int, dict[str, str]] = {}

    def _bindings_for(enc: _Def | None) -> dict[str, str]:
        if enc is None:
            return {}
        key = id(enc)
        if key not in _bind_cache:
            _bind_cache[key] = _class_bindings(
                src[enc.start:enc.end].decode("utf-8", "replace")
            )
        return _bind_cache[key]

    for caps in _matches(lang, "calls", root):
        callee = (caps.get("callee") or [None])[0]
        if callee is None:
            continue
        name = callee.text.decode("utf-8", "replace")
        if not name or name in _NOISE_CALLEES:
            continue
        enc = enclosing(callee.start_byte, callee.start_byte + 1)
        caller_slug = enc.slug if enc else file_slug
        loc = f"L{callee.start_point[0] + 1}"

        recv_name, recv_kind, recv_text = _receiver_of(callee)
        caller_class: str | None = None
        if enc is not None:
            if enc.is_class:
                caller_class = enc.name
            elif enc.parent is not None and enc.parent.is_class:
                caller_class = enc.parent.name

        # infer the receiver's class where we safely can
        recv_type: str | None = None
        if recv_kind in ("self", "self_attr"):
            recv_type = caller_class
        elif recv_kind == "ctor":
            recv_type = recv_name
        elif recv_kind == "name" and recv_name:
            recv_type = _bindings_for(enc).get(recv_name)
            if recv_type is None and recv_name in class_names:
                recv_type = recv_name  # ClassName.static() / ClassName().x

        # precise cross-file target from the import table
        import_module: str | None = None
        import_symbol: str | None = None
        if name in sym_import:
            import_module, import_symbol = sym_import[name]
        else:
            for cand in (recv_text, recv_name):
                if cand and cand in mod_alias:
                    import_module, import_symbol = mod_alias[cand], name
                    break
            else:
                if recv_name and recv_name in sym_import:
                    # `from pkg import sub` then `sub.fn()` -> pkg/sub
                    base_mod, base_sym = sym_import[recv_name]
                    import_module, import_symbol = f"{base_mod}/{base_sym}", name

        is_method = recv_kind != "bare"

        # -- same-file resolution ----------------------------------------
        # self / bare: the historical single-name match (safe, in-file).
        # x.foo() with a known receiver class: match that class's method only.
        local: _Def | None = None
        if recv_kind in ("self", "self_attr") and caller_class:
            local = methods_by_class.get(caller_class, {}).get(name)
        if local is None and recv_kind in ("bare", "self", "self_attr"):
            local = same_file_defs.get(name)
        elif local is None and recv_type:
            local = methods_by_class.get(recv_type, {}).get(name)

        if local is not None and local.slug != caller_slug:
            res.edges.append({
                "src_slug": caller_slug, "dst_slug": local.slug, "relation": "calls",
                "confidence": "EXTRACTED", "confidence_score": 1.0, "context": "call",
                "source_location": loc, "evidence": "same-file",
            })
            continue
        if local is not None:
            continue  # resolved to the caller itself (recursion) — skip

        res.raw_refs.append({
            "caller_slug": caller_slug, "callee": name, "relation": "calls",
            "source_location": loc,
            "import_evidence": name in imported,
            "lang": lang, "is_method": is_method, "recv": recv_name,
            "recv_kind": recv_kind, "recv_type": recv_type,
            "caller_class": caller_class,
            "import_module": import_module, "import_symbol": import_symbol,
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
