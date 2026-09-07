# codegraph

Code-to-queryable-graph. A behavior-compatible successor to **graphify**: same
commands, same `graph.json`, same `/codegraph` agent workflow — but a real
in-process pipeline over a SQLite store instead of an LLM orchestrating shell
steps through sidecar files.

See [`PLAN.md`](PLAN.md) for the full design and rationale.

## Status

**Complete (Phases 1–5 + hardening)** — 93 tests passing. git-tracked, CI on
Linux + Windows × py3.11/3.12, MIT-licensed, builds a clean wheel + sdist.

Commands: `extract`, `update`, `watch`, `check-update`, `query` (`--all`),
`context`, `explain`, `path`, `affected`, `god-nodes`, `diagnose`, `embed`,
`add`, `merge-graphs`, `global`, `clone`, `install`, `prs`, `save-result`,
`reflect`, `stats`, `export`, `serve` (MCP stdio **and** Streamable-HTTP/SSE).

- **Semantic (LLM) pass** — 7 backends (anthropic / openai / gemini / deepseek /
  ollama / claude-cli / none); the model annotates existing nodes by (label,
  line) and never mints an id. A prompt fingerprint re-runs it when the prompt or
  model changes.
- **Retrieval** — FTS5 stemming + rapidfuzz vocab expansion + directed graph
  expansion; opt-in embedding tier (`codegraph embed`) for true synonyms.
- **Precise resolver tiers** (both opt-in, both fall back cleanly):
  `extract --scip` ingests a `*.scip` index; `extract --lsp` drives a running
  language server (`pylsp` / `gopls` / `rust-analyzer` / …) via
  `textDocument/references`. Both emit `EXTRACTED` edges that resolve overloaded
  names the name-based guard can't.
- **Ingestion** — `codegraph add <url | arxiv | notion:ID | confluence:ID |
  file.pdf | talk.mp4>` pulls external docs into the graph (HTML/arXiv/Notion/
  Confluence in-process; PDF/Office/audio via `pdftotext` / `pandoc` / `whisper`).
- **`install --platform`** — Claude Code, Claude Desktop, Cursor, Windsurf,
  VS Code, Zed.
- **20 languages** (AST tier): Python, JavaScript, TypeScript, Go, Java, C, C++,
  Ruby, C#, Rust, Kotlin, Swift, PHP, Scala, Lua, Bash, R, Julia, Elixir,
  PowerShell. Grammars beyond the pinned ten load from `tree-sitter-language-pack`;
  a new language is one `LangConfig` entry.
- **Document tier** — Markdown / reST / AsciiDoc headings become queryable
  `section` nodes.
- **9 export formats** — `graphml`, `gexf`, `dot`, `cypher`, `csv`, `jsonl`,
  `mermaid`, `obsidian`, `tree` (plus the native `json` / `report` / `html`).
- **PR impact** — `codegraph prs` ranks open PRs by graph blast radius (via `gh`).

Out of scope (would be separate packages): Twitter/X scraping, a hosted web
dashboard.

## Install

The PyPI package is **`code-graph`**; the command and import are **`codegraph`**.

```bash
pip install code-graph        # once published
# or, right now:
pip install .                 # from a clone
pip install dist/code_graph-*.whl   # from a built wheel (python -m build)
```

All 20 languages work out of the box (grammars come from
`tree-sitter-language-pack`). Optional extras:

```bash
pip install "code-graph[langs]"        # pinned grammars for the core 10 (slightly faster)
pip install "code-graph[embeddings]"   # numpy, for `codegraph embed`
pip install -e ".[dev]"                # tests + build tooling
```

### Wire it into your AI agent(s)

```bash
codegraph install          # interactive: pick your agents, pick global/project
```

It shows a numbered list, you type e.g. `1,4,7` (or `all`), choose scope, and it
writes each agent's **MCP config** *and* its **"use codegraph first" instructions**
in that agent's own format:

| Agent | MCP config | Instructions |
|---|---|---|
| Claude Code | `~/.claude.json` / `.mcp.json` | `~/.claude/skills/codegraph/SKILL.md` |
| Claude Desktop | `claude_desktop_config.json` | — |
| Cursor | `~/.cursor/mcp.json` | `AGENTS.md` |
| VS Code (Copilot) | `.vscode/mcp.json` | `AGENTS.md` |
| Zed | `settings.json` `context_servers` | `AGENTS.md` |
| Windsurf | `~/.codeium/windsurf/mcp_config.json` | — |
| Gemini CLI | `~/.gemini/settings.json` | `/codegraph` command + `GEMINI.md` |
| Qwen Code | `~/.qwen/settings.json` | `/codegraph` command + `QWEN.md` |
| Codex CLI | `~/.codex/config.toml` | `AGENTS.md` |
| OpenCode | `opencode.json` `mcp` | `AGENTS.md` |
| Continue | `.continue/mcpServers/` | `.continue/rules/` |

Scriptable: `codegraph install --agent gemini --agent codex --scope global`.
(GLM / Kimi as *models* run through one of these — pick that agent.)
`codegraph install-skill` is the Claude-only shortcut.

### Sharing it with someone

Send them `dist/code_graph-0.6.0-py3-none-any.whl` (build it with `python -m build`).
They run:

```bash
pip install code_graph-0.6.0-py3-none-any.whl   # needs Python 3.11+
codegraph install                                # pick their agents
```

### Publishing to PyPI

```bash
python -m build
python -m twine upload dist/*        # needs a PyPI account + API token
```

## Use

```
codegraph extract path/to/project
codegraph query "how does auth work" path/to/project
codegraph affected "login" path/to/project      # what breaks if I change this
codegraph context "login" path/to/project       # body + caller/callee signatures
codegraph explain "SomeClass" path/to/project
codegraph diagnose path/to/project
codegraph export all path/to/project            # graphml, cypher, obsidian, …
```

Outputs land in `path/to/project/codegraph-out/` (`codegraph.db`, `graph.json`,
`GRAPH_REPORT.md`, `graph.html`, `exports/`). Override the directory name or
give an absolute path with `CODEGRAPH_OUT`. An existing `graphify-out/` graph
(from the old graphify skill) is reused automatically.
