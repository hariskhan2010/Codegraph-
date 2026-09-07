# Changelog

All notable changes to codegraph. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are the package
version in `pyproject.toml`.

## 0.5.0 — 2026-09-07

Feature-complete against `PLAN.md`. 87 tests.

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
