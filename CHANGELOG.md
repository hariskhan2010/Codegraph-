# Changelog

All notable changes to codegraph. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are the package
version in `pyproject.toml`.

## 0.7.0 — 2026-09-08

### Changed — receiver-aware call resolution (accuracy)
- **Schema v2.** `raw_refs` now records the call site's receiver (`is_method`,
  `recv`, `recv_kind`, `recv_type`, `caller_class`). The extractor derives this
  from the AST parent of the captured callee — no per-language query changes.
- `resolve_calls` no longer collapses every `x.method()` onto the one global
  `method()` definition. A **method call** resolves only when the receiver's
  class is known — `self`/`this` (the caller's class), a constructor
  (`Foo().bar()`), a local `x = Foo()` binding, or `Foo.bar()` — and the sole
  candidate is a method of that class (`INFERRED` 0.85, `evidence='xfile-recv'`).
- **Fan-in guard.** A method name called with an unknown receiver from ≥4 files,
  or in a built-in popular set (`get`, `execute`, `run`, `save`, …), is never
  auto-linked. On the SEO reference repo this cut the top "god node" from
  190 bogus edges (`get()`) to 0, and moved edge provenance from 85 % to 96 %
  `EXTRACTED`.
- `analyze.surprising_connections` drops weak (`<0.8`) INFERRED call edges — they
  were resolver guesses, not insights.
- `codegraph extract` now rebuilds automatically on a schema-version bump
  instead of dead-ending with "re-run extract --force".

## 0.6.0 — 2026-09-08

### Added — npm install path
- `npm/` — an npm wrapper package (`npm install -g code-graph`). Its postinstall
  downloads the standalone binary for the platform from the GitHub Release
  (no Python), falling back to `pipx install` / `pip install --user`.
- `packaging/codegraph.spec` + `.github/workflows/release.yml` — a `vX.Y.Z` tag
  builds one self-contained `codegraph` executable per OS/arch (PyInstaller) and
  attaches it, plus the wheel + sdist, to the Release.

### Added — `codegraph setup` (zero-config)
- `codegraph setup` — one command: detects every supported AI agent installed on
  the machine and wires them all (MCP + instructions, global scope), writes a
  `~/.codegraph/.setup-done` marker so it never re-runs.
- **First-run hook**: the first interactive use of any `codegraph` command offers
  to run setup ("Wire codegraph into them now? [Y/n]"). Skipped for `serve` /
  non-TTY / `CODEGRAPH_NO_SETUP=1` / once the marker exists.

### Added — interactive multi-agent installer
- `codegraph install` with no arguments prints a numbered agent picker
  (Claude Code / Desktop, Cursor, VS Code, Zed, Windsurf, Gemini CLI, Qwen Code,
  Codex CLI, OpenCode, Continue), asks global-vs-project, and writes each agent's
  **MCP config in its own format** (`mcpServers` / `servers` / `context_servers`
  map, Codex `config.toml`, OpenCode `mcp.local`, Continue standalone YAML) **and
  its instructions** (Claude `SKILL.md`, Gemini/Qwen `/codegraph` slash-command +
  `GEMINI.md`/`QWEN.md`, everyone else an `AGENTS.md` block between markers).
- Scriptable: `--agent NAME` (repeatable) `--scope {global,project}`.
- `AGENTS.md` writes are idempotent (replace between `<!-- codegraph:start/end -->`).
- MCP entry uses the bare `codegraph` command and, at global scope, `serve` with
  no path so it graphs whatever project the agent opens.
- `codegraph serve` now starts even when the project has no graph yet — tools
  return a "run `codegraph extract`" message instead of crashing.
- Bundled `AGENTS_SNIPPET.md`; `--platform` kept as an alias of `--agent`.

## 0.5.0 — 2026-09-07

Feature-complete against `PLAN.md`. 102 tests. Pip-installable
(PyPI name `code-graph`; command & import stay `codegraph`). Ships the
`/codegraph` skill — `codegraph install-skill` copies it to `~/.claude/skills/`.

### Added — skill-driven semantic pass
- `codegraph extract --semantic skill` writes `codegraph-out/semantic-request.json`
  instead of calling an API; the `/codegraph` skill (the session's own model)
  fills `semantic-response.json` and `codegraph apply-semantic` ingests it —
  free, graphify-style. Also `codegraph relabel-communities`.
- Descriptive community labels (`Signals: score, ScoringEngine`) replacing
  top-node names, with an optional LLM naming pass.
- Framework route handlers get path-qualified labels (`POST api/payments/create-session`).
- Report splits Code vs Documentation communities.

### Added — hardening (PLAN §8 ports)
- `codegraph/_util.py`: `suppressed_fds` (silences native ANSI writes from the
  Leiden partitioner that corrupt the PowerShell 5.1 scroll buffer),
  `atomic_replace` (copy-then-delete fallback for AV/editor-locked files on
  Windows), `long_path` (`\\?\` prefix past `MAX_PATH`).
- Backup-on-write: a graph carrying LLM rationale or embeddings is snapshotted to
  `graphify-out/<date>/` before a rebuild overwrites it.
- Discrete INFERRED confidence ladder `{0.55, 0.65, 0.75, 0.85, 0.95}` by signal
  strength; `quantize_confidence()` snaps extractor-supplied scores to it.
- Fixed the `graspologic` Leiden code path (was always falling back to Louvain).

### Added — Phase 5
- LSP resolver tier (`codegraph/lsp.py`, `extract --lsp`).
- `install --platform` for Cursor, Windsurf, VS Code, Zed, Claude Desktop.
- Notion (`add notion:<id>`) and Confluence (`add confluence:<id|url>`) ingestion.

## 0.4.0 — 2026-09-07 (Phase 4)
- SCIP indexer tier (`codegraph/scip.py`, `extract --scip`).
- Streamable-HTTP + SSE transport for `serve --http`.
- git merge-driver for `graphify-out/**` (`codegraph merge-driver`, `install --git`).
- `codegraph add <url | arxiv | file>` document ingestion.
- `export cypher --run` — live Neo4j / FalkorDB / Memgraph load.

## 0.3.0 — 2026-09-07 (Phase 3)
- 9 export formats: graphml, gexf, dot, cypher, csv, jsonl, mermaid, obsidian, tree.
- Document tier: Markdown / reST / AsciiDoc headings become `section` nodes.
- Languages: +R, Julia, Elixir, PowerShell (20 total).
- `clone`, `install`, `prs` (PR blast-radius via `gh`), `diagnose`,
  suggested-questions generator.

## 0.2.0 — 2026-09-07 (Phase 2)
- `codegraph context <symbol>` — body + caller/callee signatures.
- Languages: +Kotlin, Swift, PHP, Scala, Lua, Bash via `tree-sitter-language-pack`.
- Opt-in embedding tier (`codegraph embed`) for synonym retrieval.
- `merge-graphs`, `global` cross-repo registry, `query --all`.
- `check-update`, semantic prompt fingerprint, MCP HTTP transport.
- UTF-8 stdio on Windows.

## 0.1.0 — 2026-09-07 (Phase 1)
- SQLite store, transactional per-file replacement, integer-PK identity.
- tree-sitter extraction for Python, JavaScript, TypeScript, Go, Java, C, C++,
  Ruby, C#, Rust.
- FTS5 + rapidfuzz retrieval; `query`, `explain`, `path`, `affected`.
- Semantic annotation pass (7 backends); community detection; god nodes.
- `graph.json` / `GRAPH_REPORT.md` / `graph.html` renderers; MCP stdio server.
- The `/codegraph` agent skill.
