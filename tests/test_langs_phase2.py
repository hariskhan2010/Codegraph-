"""Phase 2 language coverage — Kotlin, Swift, PHP, Scala, Lua, Bash.

Grammars are resolved from tree-sitter-language-pack when a pinned module is
absent. Each case asserts the load-bearing extraction: definitions, a same-file
call edge, and (where the language has them) an inheritance raw-ref.
"""

import pytest

from codegraph.extract import extract_file

CASES = {
    "kotlin": ("g.kt", b"""package com.x
import com.y.Foo
class Greeter : Base {
    fun greet(): String { return hello() }
    fun hello(): String { return "hi" }
}
""", {"Greeter", ".greet()", ".hello()"}, (".greet()", ".hello()"), "Base"),
    "swift": ("g.swift", b"""import Foundation
class Greeter: Base {
    func greet() -> String { return hello() }
    func hello() -> String { return "hi" }
}
""", {"Greeter", ".greet()", ".hello()"}, (".greet()", ".hello()"), "Base"),
    "php": ("g.php", b"""<?php
class Greeter extends Base {
    public function greet() { return $this->hello(); }
    private function hello() { return "hi"; }
}
""", {"Greeter", ".greet()", ".hello()"}, (".greet()", ".hello()"), "Base"),
    "scala": ("g.scala", b"""package com.x
class Greeter extends Base {
  def greet(): String = hello()
  def hello(): String = "hi"
}
""", {"Greeter", ".greet()", ".hello()"}, (".greet()", ".hello()"), "Base"),
    "lua": ("g.lua", b"""function greet() return hello() end
function hello() return "hi" end
""", {"greet()", "hello()"}, ("greet()", "hello()"), None),
    "bash": ("g.sh", b"""greet() { hello; }
hello() { command_x; }
""", {"greet()", "hello()"}, ("greet()", "hello()"), None),
    "r": ("g.R", b"greet <- function(name) hello(name)\nhello <- function(n) n\n",
          {"greet()", "hello()"}, ("greet()", "hello()"), None),
    "julia": ("g.jl", b"function greet(g)\n  hello(g)\nend\nhello(n) = n\n",
              {"greet()", "hello()"}, ("greet()", "hello()"), None),
    "elixir": ("g.ex", b"defmodule App.G do\n  def greet(name), do: hello(name)\n"
               b"  def hello(n), do: n\nend\n",
               {"App.G", ".greet()", ".hello()"}, (".greet()", ".hello()"), None),
    "powershell": ("g.ps1", b"function Get-Greeting {\n  Write-Hello\n}\n"
                   b"function Write-Hello {\n  Get-Item\n}\n",
                   {"Get-Greeting()", "Write-Hello()"},
                   ("Get-Greeting()", "Write-Hello()"), None),
}


@pytest.mark.parametrize("lang", list(CASES))
def test_phase2_language_extraction(lang):
    rel, src, want_labels, want_call, want_super = CASES[lang]
    res = extract_file(rel, src, lang)
    assert res.error is None, res.error
    labels = {n["label"] for n in res.nodes}
    assert want_labels <= labels, (lang, labels)

    slug_label = {n["slug"]: n["label"] for n in res.nodes}
    call_pairs = {
        (slug_label.get(e["src_slug"]), slug_label.get(e["dst_slug"]))
        for e in res.edges if e["relation"] == "calls"
    }
    assert want_call in call_pairs, (lang, call_pairs)

    if want_super:
        supers = {r["callee"] for r in res.raw_refs if r["relation"] == "inherits"}
        assert want_super in supers, (lang, supers)
