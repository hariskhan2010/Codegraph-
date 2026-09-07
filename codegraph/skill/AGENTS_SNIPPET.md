<!-- codegraph:start -->
## codegraph — query the code graph before grepping

This project can be turned into a persistent, queryable graph with **codegraph**
(`pip install code-graph`). When you need to understand the codebase, prefer these
over reading many files — each returns a bounded answer for a fraction of the tokens:

- `codegraph query "<question>" <path>` — "how does X work" → a small subgraph with `file:line`
- `codegraph affected "<symbol>" <path>` — "what breaks if I change X" → reverse-dependency list
- `codegraph context "<symbol>" <path>` — the symbol's body + every caller/callee signature
- `codegraph explain "<symbol>" <path>` — one node and its edges
- `codegraph-out/GRAPH_REPORT.md` — god nodes, communities, entry points (repo overview)

Setup / upkeep:

- Build once: `codegraph extract <path>` (no API key needed; add `--semantic skill`
  and this agent fills `codegraph-out/semantic-request.json`, then `codegraph apply-semantic <path>`).
- After code changes: `codegraph update <path>` (fast, no LLM).
- If an MCP server named `codegraph` is configured, its tools (`query_graph`,
  `get_context`, `affected`, `god_nodes`, `shortest_path`, …) do the same thing.
<!-- codegraph:end -->
