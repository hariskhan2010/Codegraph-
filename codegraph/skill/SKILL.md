---
name: codegraph
description: "Use for any question about a codebase, its architecture, file relationships, call graph, or 'what breaks if I change X' — especially when codegraph-out/codegraph.db exists. codegraph turns a code tree into a persistent SQLite-backed graph with directed typed edges, community detection, and query/explain/path/affected/context tools; a successor to the graphify skill. This skill is thin because the codegraph package orchestrates its own pipeline."
---

# /codegraph

Turn a code tree into a navigable graph. Unlike graphify, the pipeline runs
inside a single program (`codegraph extract`) — no step-by-step orchestration,
no sidecar files. Your job here is small: build or update the graph, then answer
from it.

## Usage

```
/codegraph                       # build the graph if missing, else answer nothing — just report status
/codegraph <path>                # same, for a path
/codegraph update [<path>]       # incremental rebuild: re-parse only changed files, no LLM (graphify's `. --update`)
/codegraph <path> --update       # same — graphify muscle memory works too
/codegraph rebuild [<path>]      # full `extract --force` (re-runs the LLM pass)
/codegraph <question>            # answer a question from the graph (build first if missing)
/codegraph affected <symbol>     # what breaks if this symbol changes
/codegraph explain <symbol>      # one node + its connections
/codegraph context <symbol>      # the code to read for a task, as one bounded blob
```

## What to do

**First, read the first word of the input and dispatch:**

| Input starts with… | Do exactly this |
|---|---|
| `update` (or the args contain `--update`) | `codegraph update <path>` — print the `N nodes · N edges · N communities` line and stop. Do **not** answer a question or run anything else. This is the graphify `/graphify . --update` equivalent. |
| `rebuild` / `--force` | `codegraph extract <path> --force --semantic skill`, then do the semantic step, then stop. |
| `affected <sym>` | `codegraph affected "<sym>" <path>` |
| `explain <sym>` | `codegraph explain "<sym>" <path>` |
| `context <sym>` | `codegraph context "<sym>" <path>` |
| `path <a> <b>` | `codegraph path "<a>" "<b>" <path>` |
| nothing, or just a path | build/refresh only (steps 1 below), report the summary |
| anything else | treat as a question (steps 1 + 2) |

1. **Locate the graph.** Look for `<path>/codegraph-out/codegraph.db` (older
   graphs may still live in `<path>/graphify-out/` — codegraph reuses that dir
   automatically if it's the only one present).
   - Missing → **`codegraph extract <path> --semantic skill`** (add `-v` for
     per-file progress), then do the **semantic step** below.
   - Present → `codegraph check-update <path>`; if it reports drift, run
     `codegraph update <path>` (fast, no LLM — like `/graphify . --update`).

### The semantic step (do this after a first `extract --semantic skill`)

`--semantic skill` builds the structure and writes `codegraph-out/semantic-request.json`
instead of calling an API. **You** (this session) are the model — like graphify:

1. Read `<path>/codegraph-out/semantic-request.json`. It has `files` (each with
   `symbols` + `source`) and `communities` (each with member symbols).
2. Write `<path>/codegraph-out/semantic-response.json`:
   ```json
   {
     "annotations": { "<file>": [ {"label": "...", "line": 12,
        "rationale": "<=15 words: what it's for", "concepts": ["..."]} ] },
     "ambiguous_edges": { "<file>": [ {"src":"...","dst":"...",
        "relation":"references","why":"<=12 words"} ] },
     "community_names": { "0": "Stripe Checkout Flow", "1": "JWT Auth & Sessions" }
   }
   ```
   Use only labels from each file's `symbols` list. Community names are 2-5 words,
   Title Case, domain-role not language ("Candle Time Arithmetic"); a tests-only
   cluster → "<subject> Tests".
3. Run **`codegraph apply-semantic <path>`** — it ingests your JSON, re-clusters
   labels, and re-renders the report.

For a big repo, `semantic-request.json` can be large — it's fine to fill
`annotations` for the top files and always fill `community_names` (that's the
cheap, high-value part). Skip this whole step for `update`.

If the user has an API key set (`ANTHROPIC_API_KEY` / …), you may instead run
`codegraph extract <path> --semantic auto` and let it do the pass itself.

2. **If the input is a question**, run `codegraph query "<question>" <path>` and
   answer from the returned subgraph. Cite `source_file:source_location`. If the
   result is `No matching nodes found.`, retry once with the key nouns only.

3. **For "what depends on / what breaks if I change X"**, use
   `codegraph affected "X" <path>` — a real reverse-call traversal, not a guess.

4. **Before editing a symbol**, `codegraph context "X" <path>` returns its body
   plus every caller/callee signature with `file:line` — usually enough to make
   the change with zero extra file reads.

5. **For an overview**, read `codegraph-out/GRAPH_REPORT.md` (Summary, God Nodes,
   Communities, Surprising Connections, Suggested Questions, Import Cycles).

## Commands

| Command | Purpose |
|---|---|
| `codegraph extract <path> [--force] [-v] [--semantic skill\|auto\|none]` | full build |
| `codegraph apply-semantic <path>` | ingest the skill-written `semantic-response.json` |
| `codegraph relabel-communities <path> [names.json]` | dump community members, or apply a `{id: name}` map |
| `codegraph update <path>` | incremental rebuild (content-changed files only, no LLM) |
| `codegraph check-update <path> [--exit-code]` | report staleness without rebuilding |
| `codegraph query "<q>" <path> [--depth N] [--budget N] [--all]` | ask the graph |
| `codegraph context "<sym>" <path>` | body + caller/callee signatures for a task |
| `codegraph explain "<node>" <path>` | describe one node |
| `codegraph affected "<node>" <path> [--depth N]` | reverse-dependency traversal |
| `codegraph path "<a>" "<b>" <path>` | shortest path |
| `codegraph god-nodes <path> [--top N] [--json]` | most-connected nodes |
| `codegraph diagnose <path>` | graph-health check (parse coverage, xref recall, staleness) |
| `codegraph embed <path> [--backend NAME]` | opt-in node embeddings for synonym retrieval |
| `codegraph merge-graphs <roots…> -o <out>` | combine several project graphs |
| `codegraph global {list,add,remove}` | cross-repo registry (`query --all`) |
| `codegraph clone <src> <dest>` | copy a graph to a new root |
| `codegraph add <url\|arxiv\|file> <path>` | ingest an external doc/paper/transcript into the graph |
| `codegraph prs [N] <path>` | rank open PRs by graph blast radius (needs `gh`) |
| `codegraph export {json,report,html,graphml,gexf,dot,cypher,csv,jsonl,mermaid,obsidian,tree,all} <path>` | re-render / export (`cypher --run` loads a live Neo4j) |
| `codegraph install [<path>]` | interactive: wire codegraph into your AI agent(s) — MCP + instructions, global or project (`--agent NAME --scope global` to script; `--git` adds the merge driver) |
| `codegraph install-skill` | Claude-only shortcut for the `/codegraph` skill |
| `codegraph serve <path> [--http --port N]` | MCP server (stdio or Streamable-HTTP/SSE) |

## Notes

- Outputs land in `<path>/codegraph-out/`: `codegraph.db` (source of truth),
  `graph.json`, `GRAPH_REPORT.md`, `graph.html`, `exports/`, `sources/`.
  Override the dir with `CODEGRAPH_OUT` (a name or an absolute path).
- Languages (AST tier): Python, JavaScript, TypeScript, Go, Java, C, C++, Ruby,
  C#, Rust, Kotlin, Swift, PHP, Scala, Lua, Bash, R, Julia, Elixir, PowerShell.
  Grammars beyond the pinned ten come from `tree-sitter-language-pack`
  (`pip install codegraph[all]`).
- Markdown / reST / AsciiDoc headings are indexed as `section` nodes by default
  (`extract --no-docs` to skip), so `query` reaches design notes too.
- Precise resolver tiers: `extract --scip` (auto when a `*.scip` index + `scip`
  CLI are present) and `extract --lsp` (drives `pylsp`/`gopls`/`rust-analyzer`/… —
  precise, slower). Both emit `evidence='scip'`/`evidence='lsp'` edges and no-op
  cleanly when the tool is absent.
- `codegraph add` also takes `notion:<id>` and `confluence:<id-or-url>` (needs
  `$NOTION_TOKEN` / `$CONFLUENCE_BASE_URL`+`$CONFLUENCE_TOKEN`).
- Semantic (LLM) annotation: `--semantic anthropic|openai|gemini|ollama|claude-cli`
  or `--no-semantic`. The model never mints node ids — it annotates existing
  nodes by (label, line); unmatched annotations are dropped.
- Provenance: every edge is `EXTRACTED` (structural fact) / `INFERRED` (single
  unambiguous candidate) / `AMBIGUOUS` (LLM). Report which when it matters.
- Everything in `E:\codegraph\PLAN.md` is implemented (Phases 1–5); only Twitter/X
  scraping and a hosted dashboard are out of scope.
