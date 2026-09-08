<div align="center">

# codegraph

**Turn a codebase into a graph your AI agent queries instead of grepping.**

A persistent, SQLite-backed graph of every symbol, call, import and community in
your repo — built once, updated incrementally, served over MCP. Your agent asks
*"how does auth work"* or *"what breaks if I change `login`"* and gets a bounded,
`file:line`-precise answer in ~2k tokens instead of reading twenty files.

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![tests](https://img.shields.io/badge/tests-150%20passing-brightgreen)](tests/)

[Quickstart](#quickstart) · [Why](#why-a-graph-beats-reading-files) · [Agent setup](#use-it-with-your-ai-agent) · [Install](#install) · [Design](PLAN.md) · [Benchmark](BENCHMARK.md)

</div>

---

## The 30-second version

A coding agent spends most of its context window *locating* things — grep, open
5 files, trace a call, repeat. codegraph does that work **once, offline**, stores
the compressed result, and hands the agent a precise slice on demand.

It's a real in-process pipeline (tree-sitter → SQLite → retrieval), not an LLM
following a runbook. Every edge is tagged with honest provenance —
`EXTRACTED` (structural fact), `INFERRED` (single unambiguous candidate),
`AMBIGUOUS` (LLM) — so the agent knows what to trust.

**How accurate is "EXTRACTED"?** On a 192-file repo, take every `EXTRACTED` call
edge whose target is a common method name (`get`, `run`, `execute`, …), open the
source line, and count the ones that are *provably impossible*:

| | edges checked | provably wrong | error rate |
|---|---:|---:|---:|
| graphify | 33 | 18 | **55%** |
| **codegraph** | 33 | **0** | **0%** |

Full method & reproduction in [`BENCHMARK.md`](BENCHMARK.md).

---

## Quickstart

```bash
pipx install code-graph          # command + import are `codegraph`
cd your/project
codegraph extract .              # build the graph  (add a URL to clone & build)
codegraph query "how does session auth work"
codegraph affected "validate_token"      # what depends on this
codegraph context "validate_token"       # its body + every caller/callee signature
```

Outputs land in `your/project/codegraph-out/` — `codegraph.db` (source of truth),
`graph.json`, `GRAPH_REPORT.md`, `graph.html`. After code changes:
`codegraph update .` (fast, no LLM).

---

## Why a graph beats reading files

> The graph ships **conclusions, not evidence.**

- **One `query` replaces grep + 5 file reads.** The "which files matter" work was
  done offline; the agent gets a ~2k-token subgraph.
- **An edge is ~15 tokens; the fact behind it is hundreds.**
  `handle_request --calls--> login [EXTRACTED]` vs. reading both function bodies.
- **`affected` collapses an impact analysis into one call** — a real reverse-call
  traversal, not "grep the name and hope".
- **Every node has a line range**, so the agent reads 25 lines, not an 800-line file.
- **Provenance lets the agent skip verification** — trust `EXTRACTED`, only open
  the file for `INFERRED` / `AMBIGUOUS`.
- **`GRAPH_REPORT.md` replaces the orientation phase** — god nodes, communities,
  entry points, suggested questions, in a few hundred tokens.
- **Deterministic output** — byte-stable turn to turn, so a graph pinned in a
  prompt keeps the prompt cache warm.

Rough shape on *"how does X work"* for a medium repo: **~5–8× fewer tokens** and
**far fewer round trips**. The catch: it only pays off if the agent is *told* to
query first — hence the `/codegraph` skill and the MCP server.

---

## What you get

**Extraction & resolution**
- 20 languages (AST tier): Python, JS, TS, Go, Java, C, C++, Ruby, C#, Rust,
  Kotlin, Swift, PHP, Scala, Lua, Bash, R, Julia, Elixir, PowerShell — a new one
  is a single config entry.
- **Receiver-aware call resolution** — `session.execute()` and a bare `execute()`
  are told apart across all 20 grammars, so common method names don't collapse
  into manufactured god nodes.
- **Path-precise import resolution** → ~99% of edges land as `EXTRACTED`.
- Opt-in precise tiers: `extract --scip` (ingest a SCIP index) and
  `extract --lsp` (drive a running language server). Both no-op cleanly when absent.

**Retrieval**
- FTS5 stemming + rapidfuzz vocab expansion + directed graph expansion + rerank —
  `query "authentication"` finds `login` with no LLM round-trip.
- Opt-in embedding tier (`codegraph embed`) for true synonyms.
- `query` / `explain` / `path` / `affected` / `context` / `god-nodes`.

**Beyond code**
- Document tier — Markdown / reST / AsciiDoc headings become queryable nodes.
- Media tier — images get a vision-derived concept; audio/video are transcribed
  and indexed. Never a hard failure when a transcriber is missing.
- Cross-doc **idea graph** — `concept` nodes for named principles/decisions,
  linked by `semantically_similar_to` across documents.
- Ingestion — `codegraph add <url | arxiv | notion:ID | confluence:ID | file.pdf>`.

**Semantic pass (optional, no API key needed)**
- `extract --semantic skill` writes a work order; the `/codegraph` agent
  annotates it (rationale, `AMBIGUOUS` edges, community names) and
  `codegraph apply-semantic` folds it back in — graphify-style, for free.
- Or point it at a backend: `--semantic anthropic|openai|gemini|ollama|…`.

**Outputs**
- `graph.json` (GraphRAG-compatible), `GRAPH_REPORT.md`, self-contained `graph.html`.
- Exports: `graphml`, `gexf`, `dot`, `cypher`, `csv`, `jsonl`, `mermaid`,
  `obsidian`, `wiki`, `tree`. `codegraph export cypher --run` loads a live Neo4j.

**Serving**
- MCP server — stdio **and** Streamable-HTTP/SSE. graphify-compatible tool names.
- `codegraph prs` — ranks open PRs by graph blast radius (via `gh`).
- `codegraph diagnose` — parse coverage, cross-ref recall, staleness, provenance.

---

## Use it with your AI agent

```bash
codegraph setup
```

One command. It detects the AI CLIs/editors on your machine and wires each one —
**MCP config** *and* a *"query the graph first"* instruction file, in that
agent's own format. (The first time you run any `codegraph` command it offers to
do this.)

| Agent | MCP config | Instructions |
|---|---|---|
| Claude Code / Desktop | `~/.claude.json` · `.mcp.json` | `~/.claude/skills/codegraph/SKILL.md` |
| Cursor · VS Code · Zed · Windsurf | each tool's MCP file | `AGENTS.md` |
| Gemini CLI · Qwen Code | `~/.gemini` · `~/.qwen` `settings.json` | `/codegraph` command + `GEMINI.md`/`QWEN.md` |
| Codex CLI · OpenCode · Continue | `config.toml` · `opencode.json` · `.continue/` | `AGENTS.md` / rules |

Hand-pick with `codegraph install` (interactive) or script it:
`codegraph install --agent gemini --agent codex --scope global`.

---

## Install

```bash
pipx install code-graph          # recommended — isolated global install
pip install code-graph
npm install -g code-graph        # Node people — bootstraps a binary / pip
```

From a clone:

```bash
pip install .
pip install -e ".[dev]"          # + tests & build tooling
```

All 20 languages work out of the box (`tree-sitter-language-pack`). Extras:
`code-graph[langs]` (pinned core-10 grammars), `code-graph[embeddings]` (numpy),
`code-graph[media]` (audio/video transcription).

> The PyPI/npm package is **`code-graph`**; the command and `import` are
> **`codegraph`** (plain `codegraph` was already taken on both).

---

## How it works

```
codegraph extract <path|git-url>
  ├─ detect    walk, .gitignore, 3-stage secret skip, classify code|doc|media
  ├─ ast       tree-sitter per file → nodes + edges + unresolved refs   (parallel)
  ├─ resolve   cross-file calls / imports / inheritance — one global pass
  ├─ scip/lsp  optional precise tiers
  ├─ semantic  optional — LLM (or the /codegraph skill) annotates existing nodes
  ├─ cluster   Leiden / Louvain, deterministic, stable community ids
  ├─ analyze   god nodes, surprising connections, import cycles
  └─ render    graph.json + GRAPH_REPORT.md + graph.html   (all from the DB)
```

The SQLite store is the fix for graphify's structural bugs: node identity is an
integer PK (not a path-string two passes must re-derive), incremental update is a
`DELETE + reinsert` of one file's rows in a single transaction (no manifest
diffing, no shrink guards), and `graph.json` is a deterministic `SELECT`.

Full design, rationale, and the graphify bug analysis: **[`PLAN.md`](PLAN.md)**.

---

## Relationship to graphify

codegraph is a behavior-compatible successor to the **graphify** skill — same
commands, same `graph.json` contract, same `/codegraph` workflow — but a real
program over a database instead of an LLM orchestrating shell steps through
sidecar files. `graphify-out/` graphs are picked up automatically.

Pairs with **[carryover](https://pypi.org/project/carryover/)**: codegraph =
*how the code is wired*, carryover = *where this working session is*.

---

## Docs

- [`PLAN.md`](PLAN.md) — full design, the pipeline, and the graphify bug analysis
- [`BENCHMARK.md`](BENCHMARK.md) — reproducible head-to-head vs graphify
- [`CHANGELOG.md`](CHANGELOG.md) — what landed in each version

## License

MIT — see [`LICENSE`](LICENSE).
