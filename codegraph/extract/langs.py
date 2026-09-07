"""Per-language tree-sitter configuration.

Each :class:`LangConfig` carries the grammar module name plus tree-sitter query
strings for definitions, calls, and imports. Language-specific parsing of import
targets and superclass lists lives in small ``supers`` / ``imports`` callables so
the generic walker in :mod:`codegraph.extract.engine` stays language-agnostic.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable

from tree_sitter import Language, Node, Parser, Query

# --------------------------------------------------------------------------- #


@dataclass
class LangConfig:
    name: str
    module: str
    lang_symbol: str = "language"
    pack_name: str = ""            # tree_sitter_language_pack key, if the pinned module is absent
    # capture @fn/@name for functions & methods; @cls/@name for types
    functions_query: str = ""
    classes_query: str = ""
    calls_query: str = ""       # capture @callee (identifier) [@recv optional]
    imports_query: str = ""     # capture @import (whole statement node)
    class_node_types: tuple[str, ...] = ()
    func_node_types: tuple[str, ...] = ()
    supers: Callable[[Node], list[str]] | None = None
    import_names: Callable[[Node, bytes], list[str]] | None = None
    family: str = ""


# ------- superclass / import helpers --------------------------------------- #


def _txt(n: Node, src: bytes) -> str:
    return src[n.start_byte : n.end_byte].decode("utf-8", "replace")


def _py_supers(cls: Node) -> list[str]:
    arglist = cls.child_by_field_name("superclasses")
    if not arglist:
        return []
    out = []
    for ch in arglist.named_children:
        if ch.type in ("identifier", "attribute"):
            out.append(ch.text.decode("utf-8", "replace").split(".")[-1])
    return out


def _py_imports(node: Node, src: bytes) -> list[str]:
    names: list[str] = []
    if node.type == "import_statement":
        for ch in node.named_children:
            if ch.type == "dotted_name":
                names.append(_txt(ch, src).split(".")[0])
            elif ch.type == "aliased_import":
                dn = ch.child_by_field_name("name")
                if dn:
                    names.append(_txt(dn, src).split(".")[0])
    elif node.type == "import_from_statement":
        mod = node.child_by_field_name("module_name")
        if mod:
            names.append(_txt(mod, src).strip(".").split(".")[-1])
        for ch in node.named_children:
            if ch.type == "dotted_name" and ch != mod:
                names.append(_txt(ch, src).split(".")[-1])
            elif ch.type == "aliased_import":
                nm = ch.child_by_field_name("name")
                if nm:
                    names.append(_txt(nm, src).split(".")[-1])
    return [n for n in names if n]


def _generic_supers_field(field_names: tuple[str, ...]):
    def _f(cls: Node) -> list[str]:
        out: list[str] = []
        for fn in field_names:
            fld = cls.child_by_field_name(fn)
            if not fld:
                continue
            stack = [fld]
            while stack:
                n = stack.pop()
                if n.type in ("identifier", "type_identifier", "scoped_type_identifier",
                              "generic_type", "qualified_type"):
                    out.append(n.text.decode("utf-8", "replace").split(".")[-1].split("<")[0])
                else:
                    stack.extend(n.named_children)
        return out
    return _f


def _supers_by_child_types(*types: str):
    """Superclass names = the text of every ``type_identifier``-ish node that is a
    (possibly nested) child of a node whose type is in ``types``."""
    want = set(types)

    def _f(cls: Node) -> list[str]:
        out: list[str] = []
        stack = list(cls.named_children)
        while stack:
            n = stack.pop()
            if n.type in want:
                inner = [n]
                while inner:
                    m = inner.pop()
                    if m.type in ("type_identifier", "identifier", "simple_identifier",
                                  "user_type", "name"):
                        txt = m.text.decode("utf-8", "replace").split(".")[-1].split("<")[0]
                        if txt:
                            out.append(txt)
                        break
                    inner.extend(m.named_children)
            else:
                stack.extend(n.named_children)
        return out

    return _f


def _import_tail_only(node: Node, src: bytes) -> list[str]:
    """Just the last identifier of a dotted import path (``com.y.Foo`` -> ``Foo``).
    Used where :func:`_import_last_ident` would over-collect namespace segments."""
    if node.type in ("alias", "identifier", "type_identifier", "simple_identifier",
                     "scoped_identifier", "dotted_name", "name"):
        tail = _txt(node, src).strip("\"'`").replace("::", ".").split(".")[-1]
        return [tail] if tail and tail.isidentifier() else []
    idents = [
        _txt(n, src)
        for n in _walk_named(node)
        if n.type in ("identifier", "type_identifier", "simple_identifier", "name")
    ]
    if not idents:
        return []
    tail = idents[-1].strip("\"'`").split(".")[-1]
    return [tail] if tail and tail.isidentifier() else []


def _walk_named(node: Node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(reversed(n.named_children))


def _import_last_ident(node: Node, src: bytes) -> list[str]:
    # crude but effective: take every identifier-ish token in the import stmt
    out: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type in ("identifier", "type_identifier", "package_identifier",
                      "namespace", "scoped_identifier", "dotted_name",
                      "string_literal", "interpreted_string_literal", "string"):
            t = _txt(n, src).strip("\"'`").replace("\\", "/")
            seg = t.split("/")[-1].split(".")[-1]
            if seg and seg.isidentifier():
                out.append(seg)
        stack.extend(n.named_children)
    return out


# ------- configs ---------------------------------------------------------- #

_PY = LangConfig(
    name="python", module="tree_sitter_python", family="python",
    functions_query="(function_definition name: (identifier) @name) @fn",
    classes_query="(class_definition name: (identifier) @name) @cls",
    calls_query="""
        (call function: (identifier) @callee)
        (call function: (attribute attribute: (identifier) @callee) )
    """,
    imports_query="(import_statement) @import (import_from_statement) @import",
    class_node_types=("class_definition",),
    func_node_types=("function_definition",),
    supers=_py_supers, import_names=_py_imports,
)

_JS = LangConfig(
    name="javascript", module="tree_sitter_javascript", family="jsts",
    functions_query="""
        (function_declaration name: (identifier) @name) @fn
        (method_definition name: (property_identifier) @name) @fn
        (variable_declarator name: (identifier) @name value: (arrow_function)) @fn
        (variable_declarator name: (identifier) @name value: (function_expression)) @fn
    """,
    classes_query="(class_declaration name: (identifier) @name) @cls",
    calls_query="""
        (call_expression function: (identifier) @callee)
        (call_expression function: (member_expression property: (property_identifier) @callee))
    """,
    imports_query="(import_statement) @import (call_expression function: (identifier) @req (#eq? @req \"require\")) @import",
    class_node_types=("class_declaration",),
    func_node_types=("function_declaration", "method_definition", "arrow_function",
                     "function_expression"),
    supers=_generic_supers_field(("superclass",)),
    import_names=_import_last_ident,
)

_TS = LangConfig(
    name="typescript", module="tree_sitter_typescript", lang_symbol="language_typescript",
    family="jsts",
    functions_query="""
        (function_declaration name: (identifier) @name) @fn
        (method_definition name: (property_identifier) @name) @fn
        (variable_declarator name: (identifier) @name value: (arrow_function)) @fn
        (public_field_definition name: (property_identifier) @name value: (arrow_function)) @fn
    """,
    classes_query="""
        (class_declaration name: (type_identifier) @name) @cls
        (interface_declaration name: (type_identifier) @name) @cls
        (enum_declaration name: (identifier) @name) @cls
    """,
    calls_query="""
        (call_expression function: (identifier) @callee)
        (call_expression function: (member_expression property: (property_identifier) @callee))
    """,
    imports_query="(import_statement) @import",
    class_node_types=("class_declaration", "interface_declaration", "enum_declaration"),
    func_node_types=("function_declaration", "method_definition", "arrow_function"),
    supers=_generic_supers_field(("superclass",)),
    import_names=_import_last_ident,
)

_GO = LangConfig(
    name="go", module="tree_sitter_go", family="go",
    functions_query="""
        (function_declaration name: (identifier) @name) @fn
        (method_declaration name: (field_identifier) @name) @fn
    """,
    classes_query="""
        (type_declaration (type_spec name: (type_identifier) @name type: (struct_type))) @cls
        (type_declaration (type_spec name: (type_identifier) @name type: (interface_type))) @cls
    """,
    calls_query="""
        (call_expression function: (identifier) @callee)
        (call_expression function: (selector_expression field: (field_identifier) @callee))
    """,
    imports_query="(import_declaration) @import",
    class_node_types=("type_declaration",),
    func_node_types=("function_declaration", "method_declaration"),
    supers=None, import_names=_import_last_ident,
)

_JAVA = LangConfig(
    name="java", module="tree_sitter_java", family="jvm",
    functions_query="""
        (method_declaration name: (identifier) @name) @fn
        (constructor_declaration name: (identifier) @name) @fn
    """,
    classes_query="""
        (class_declaration name: (identifier) @name) @cls
        (interface_declaration name: (identifier) @name) @cls
        (enum_declaration name: (identifier) @name) @cls
        (record_declaration name: (identifier) @name) @cls
    """,
    calls_query="(method_invocation name: (identifier) @callee)",
    imports_query="(import_declaration) @import",
    class_node_types=("class_declaration", "interface_declaration", "enum_declaration",
                      "record_declaration"),
    func_node_types=("method_declaration", "constructor_declaration"),
    supers=_generic_supers_field(("superclass", "interfaces")),
    import_names=_import_last_ident,
)

_C = LangConfig(
    name="c", module="tree_sitter_c", family="native",
    functions_query="(function_definition declarator: (function_declarator declarator: (identifier) @name)) @fn",
    classes_query="""
        (struct_specifier name: (type_identifier) @name body: (field_declaration_list)) @cls
        (enum_specifier name: (type_identifier) @name) @cls
    """,
    calls_query="(call_expression function: (identifier) @callee)",
    imports_query="(preproc_include) @import",
    class_node_types=("struct_specifier", "enum_specifier"),
    func_node_types=("function_definition",),
    supers=None, import_names=_import_last_ident,
)

_CPP = LangConfig(
    name="cpp", module="tree_sitter_cpp", family="native",
    functions_query="""
        (function_definition declarator: (function_declarator declarator: (identifier) @name)) @fn
        (function_definition declarator: (function_declarator declarator: (field_identifier) @name)) @fn
        (function_definition declarator: (function_declarator declarator: (qualified_identifier name: (identifier) @name))) @fn
    """,
    classes_query="""
        (class_specifier name: (type_identifier) @name) @cls
        (struct_specifier name: (type_identifier) @name body: (field_declaration_list)) @cls
    """,
    calls_query="""
        (call_expression function: (identifier) @callee)
        (call_expression function: (field_expression field: (field_identifier) @callee))
        (call_expression function: (qualified_identifier name: (identifier) @callee))
    """,
    imports_query="(preproc_include) @import",
    class_node_types=("class_specifier", "struct_specifier"),
    func_node_types=("function_definition",),
    supers=_generic_supers_field(("base_class_clause",)),
    import_names=_import_last_ident,
)

_RUBY = LangConfig(
    name="ruby", module="tree_sitter_ruby", family="ruby",
    functions_query="""
        (method name: (identifier) @name) @fn
        (singleton_method name: (identifier) @name) @fn
    """,
    classes_query="""
        (class name: (constant) @name) @cls
        (module name: (constant) @name) @cls
    """,
    calls_query="""
        (call method: (identifier) @callee)
        (method_call method: (identifier) @callee)
    """,
    imports_query="(call method: (identifier) @m (#match? @m \"require|require_relative|load\")) @import",
    class_node_types=("class", "module"),
    func_node_types=("method", "singleton_method"),
    supers=_generic_supers_field(("superclass",)),
    import_names=_import_last_ident,
)

_CS = LangConfig(
    name="csharp", module="tree_sitter_c_sharp", family="dotnet",
    functions_query="""
        (method_declaration name: (identifier) @name) @fn
        (constructor_declaration name: (identifier) @name) @fn
        (local_function_statement name: (identifier) @name) @fn
    """,
    classes_query="""
        (class_declaration name: (identifier) @name) @cls
        (interface_declaration name: (identifier) @name) @cls
        (struct_declaration name: (identifier) @name) @cls
        (record_declaration name: (identifier) @name) @cls
        (enum_declaration name: (identifier) @name) @cls
    """,
    calls_query="""
        (invocation_expression function: (identifier) @callee)
        (invocation_expression function: (member_access_expression name: (identifier) @callee))
    """,
    imports_query="(using_directive) @import",
    class_node_types=("class_declaration", "interface_declaration", "struct_declaration",
                      "record_declaration", "enum_declaration"),
    func_node_types=("method_declaration", "constructor_declaration",
                     "local_function_statement"),
    supers=_generic_supers_field(("bases",)),
    import_names=_import_last_ident,
)

_RUST = LangConfig(
    name="rust", module="tree_sitter_rust", family="rust",
    functions_query="(function_item name: (identifier) @name) @fn",
    classes_query="""
        (struct_item name: (type_identifier) @name) @cls
        (enum_item name: (type_identifier) @name) @cls
        (trait_item name: (type_identifier) @name) @cls
    """,
    calls_query="""
        (call_expression function: (identifier) @callee)
        (call_expression function: (scoped_identifier name: (identifier) @callee))
        (call_expression function: (field_expression field: (field_identifier) @callee))
    """,
    imports_query="(use_declaration) @import",
    class_node_types=("struct_item", "enum_item", "trait_item"),
    func_node_types=("function_item",),
    supers=None, import_names=_import_last_ident,
)

# --------------------------------------------------------------------------- #
# Phase 2 languages — grammars come from tree-sitter-language-pack when the
# individually-pinned module is not installed (see get_language fallback).
# --------------------------------------------------------------------------- #

_KOTLIN = LangConfig(
    name="kotlin", module="tree_sitter_kotlin", pack_name="kotlin", family="jvm",
    functions_query="(function_declaration (simple_identifier) @name) @fn",
    classes_query="""
        (class_declaration (type_identifier) @name) @cls
        (object_declaration (type_identifier) @name) @cls
    """,
    calls_query="""
        (call_expression (simple_identifier) @callee)
        (call_expression (navigation_expression (navigation_suffix (simple_identifier) @callee)))
    """,
    imports_query="(import_header (identifier) @import)",
    class_node_types=("class_declaration", "object_declaration"),
    func_node_types=("function_declaration",),
    supers=_supers_by_child_types("delegation_specifier"),
    import_names=_import_tail_only,
)

_SWIFT = LangConfig(
    name="swift", module="tree_sitter_swift", pack_name="swift", family="swift",
    functions_query="(function_declaration (simple_identifier) @name) @fn",
    classes_query="""
        (class_declaration name: (type_identifier) @name) @cls
        (class_declaration (type_identifier) @name) @cls
        (protocol_declaration name: (type_identifier) @name) @cls
    """,
    calls_query="""
        (call_expression (simple_identifier) @callee)
        (call_expression (navigation_expression (navigation_suffix (simple_identifier) @callee)))
    """,
    imports_query="(import_declaration (identifier) @import)",
    class_node_types=("class_declaration", "protocol_declaration"),
    func_node_types=("function_declaration",),
    supers=_supers_by_child_types("inheritance_specifier"),
    import_names=_import_last_ident,
)

_PHP = LangConfig(
    name="php", module="tree_sitter_php", pack_name="php", lang_symbol="language_php",
    family="php",
    functions_query="""
        (function_definition name: (name) @name) @fn
        (method_declaration name: (name) @name) @fn
    """,
    classes_query="""
        (class_declaration name: (name) @name) @cls
        (interface_declaration name: (name) @name) @cls
        (trait_declaration name: (name) @name) @cls
        (enum_declaration name: (name) @name) @cls
    """,
    calls_query="""
        (function_call_expression function: (name) @callee)
        (function_call_expression (name) @callee)
        (member_call_expression name: (name) @callee)
        (scoped_call_expression name: (name) @callee)
    """,
    imports_query="(namespace_use_declaration) @import",
    class_node_types=("class_declaration", "interface_declaration", "trait_declaration",
                      "enum_declaration"),
    func_node_types=("function_definition", "method_declaration"),
    supers=_supers_by_child_types("base_clause", "class_interface_clause"),
    import_names=_import_last_ident,
)

_SCALA = LangConfig(
    name="scala", module="tree_sitter_scala", pack_name="scala", family="jvm",
    functions_query="(function_definition name: (identifier) @name) @fn",
    classes_query="""
        (class_definition name: (identifier) @name) @cls
        (object_definition name: (identifier) @name) @cls
        (trait_definition name: (identifier) @name) @cls
    """,
    calls_query="""
        (call_expression function: (identifier) @callee)
        (call_expression function: (field_expression field: (identifier) @callee))
    """,
    imports_query="(import_declaration) @import",
    class_node_types=("class_definition", "object_definition", "trait_definition"),
    func_node_types=("function_definition",),
    supers=_supers_by_child_types("extends_clause"),
    import_names=_import_tail_only,
)

_LUA = LangConfig(
    name="lua", module="tree_sitter_lua", pack_name="lua", family="lua",
    functions_query="""
        (function_declaration name: (identifier) @name) @fn
        (function_declaration name: (dot_index_expression field: (identifier) @name)) @fn
        (function_declaration name: (method_index_expression method: (identifier) @name)) @fn
    """,
    classes_query="",
    calls_query="""
        (function_call name: (identifier) @callee)
        (function_call name: (dot_index_expression field: (identifier) @callee))
        (function_call name: (method_index_expression method: (identifier) @callee))
    """,
    imports_query="",
    func_node_types=("function_declaration",),
    supers=None, import_names=None,
)

_BASH = LangConfig(
    name="bash", module="tree_sitter_bash", pack_name="bash", family="shell",
    functions_query="(function_definition name: (word) @name) @fn",
    classes_query="",
    calls_query="(command name: (command_name (word) @callee))",
    imports_query="""
        (command name: (command_name (word) @c) argument: (word) @import
                 (#match? @c "^(source|\\.)$"))
    """,
    func_node_types=("function_definition",),
    supers=None, import_names=_import_last_ident,
)

_R = LangConfig(
    name="r", module="tree_sitter_r", pack_name="r", family="r",
    functions_query="""
        (binary_operator lhs: (identifier) @name rhs: (function_definition)) @fn
    """,
    classes_query="",
    calls_query="(call function: (identifier) @callee)",
    imports_query="",
    func_node_types=("function_definition",),
    supers=None, import_names=None,
)

_JULIA = LangConfig(
    name="julia", module="tree_sitter_julia", pack_name="julia", family="julia",
    functions_query="""
        (function_definition (signature (call_expression (identifier) @name))) @fn
        (assignment (call_expression (identifier) @name)) @fn
    """,
    classes_query="(struct_definition (type_head (identifier) @name)) @cls",
    calls_query="(call_expression (identifier) @callee)",
    imports_query="""
        (import_statement (identifier) @import)
        (using_statement (identifier) @import)
    """,
    class_node_types=("struct_definition",),
    func_node_types=("function_definition", "assignment"),
    supers=None, import_names=_import_tail_only,
)

_ELIXIR = LangConfig(
    name="elixir", module="tree_sitter_elixir", pack_name="elixir", family="beam",
    functions_query="""
        (call target: ((identifier) @_d (#match? @_d "^defp?$"))
              (arguments (call target: (identifier) @name))) @fn
        (call target: ((identifier) @_d (#match? @_d "^defp?$"))
              (arguments (identifier) @name)) @fn
    """,
    classes_query="""
        (call target: ((identifier) @_d (#match? @_d "^(defmodule|defprotocol)$"))
              (arguments (alias) @name)) @cls
    """,
    calls_query="(call target: (identifier) @callee)",
    imports_query="""
        (call target: ((identifier) @_i (#match? @_i "^(import|alias|require|use)$"))
              (arguments (alias) @import))
    """,
    class_node_types=("call",),
    func_node_types=("call",),
    supers=None, import_names=_import_tail_only,
)

_POWERSHELL = LangConfig(
    name="powershell", module="tree_sitter_powershell", pack_name="powershell",
    family="shell",
    functions_query="(function_statement (function_name) @name) @fn",
    classes_query="(class_statement (simple_name) @name) @cls",
    calls_query="(command command_name: (command_name) @callee)",
    imports_query="",
    func_node_types=("function_statement",),
    supers=None, import_names=None,
)

LANGS: dict[str, LangConfig] = {
    c.name: c
    for c in (_PY, _JS, _TS, _GO, _JAVA, _C, _CPP, _RUBY, _CS, _RUST,
              _KOTLIN, _SWIFT, _PHP, _SCALA, _LUA, _BASH,
              _R, _JULIA, _ELIXIR, _POWERSHELL)
}


def _pack_language(name: str):
    try:
        from tree_sitter_language_pack import get_language as _pack_get  # type: ignore
    except Exception:
        return None
    try:
        return _pack_get(name)
    except Exception:
        return None


@lru_cache(maxsize=None)
def get_language(name: str) -> Language | None:
    cfg = LANGS.get(name)
    if not cfg:
        return None
    try:
        mod = importlib.import_module(cfg.module)
        fn = getattr(mod, cfg.lang_symbol)
        return Language(fn())
    except Exception:
        pass
    lang = _pack_language(cfg.pack_name or cfg.name)
    # tree_sitter_language_pack returns a ready Language object
    return lang


@lru_cache(maxsize=None)
def get_parser(name: str) -> Parser | None:
    lang = get_language(name)
    return Parser(lang) if lang else None


@lru_cache(maxsize=None)
def compiled_query(name: str, which: str) -> Query | None:
    lang = get_language(name)
    cfg = LANGS.get(name)
    if not lang or not cfg:
        return None
    src = getattr(cfg, f"{which}_query", "")
    if not src.strip():
        return None
    try:
        return Query(lang, src)
    except Exception:
        return None
