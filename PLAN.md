# codegraph — design & implementation plan

> A behavior-compatible successor to **graphify** that keeps everything good about it
> and fixes the structural problems that generate its bug tail.
>
> Status: **Complete — Phases 1–5 + hardening** (2026-09-07). v0.5.0, 93 tests,
> git-tracked with CI, LICENSE, CHANGELOG. Design reconstructed from the research
> session (graphify `graphifyy` 0.9.54 internals + 3 deep-dive agents).
> 20 languages, SCIP + LSP resolver tiers, 9 export formats + live Neo4j load,
> document + URL/arXiv/PDF/Notion/Confluence ingestion, MCP (stdio +
> Streamable-HTTP/SSE), embeddings, cross-repo registry, PR impact, git
> merge-driver, 6-platform `install`, all PLAN §8 robustness ports. §9 end lists
> the only remaining items (would each be a separate package).

---

## 0. What this is

graphify turns a folder (code, docs, PDFs, images, video) into a knowledge graph with
three outputs — interactive HTML, a GraphRAG-style `graph.json`, and a plain-language
`GRAPH_REPORT.md` — plus community detection ("god nodes", "surprising connections"),
a lexical query/path/explain layer, an MCP server, and ~10 export targets.

**codegraph = same surface, same commands, same `/codegraph` agent workflow, same
`graph.json` on disk — but a real pipeline instead of an LLM reading a 750-line runbook,
a database instead of a JSON blob, and real retrieval instead of substring matching.**

### The core diagnosis

graphify is *an LLM orchestrating a fragile shell pipeline over a heuristic extractor,
queried lexically.* Four things generate most of its bug tail (trail runs to #3028):

| # | Root cause | Symptom cluster |
|---|---|---|
| 1 | **Orchestration is prose.** SKILL.md drives ~10 steps by pasting PowerShell heredocs; state passes through `graphify-out/` dot-file sidecars (`.graphify_python`, `.graphify_detect.json`, `.graphify_ast.json`, `.graphify_chunk_NN.json`, …). | BOM breaks the hook (#3028), console-encoding drift corrupts sidecar JSON (#2528), Whisper stdout corrupts JSON (#1392), ANSI codes break PowerShell scroll (#19), `--cluster-only` breaks after cleanup (#1392). "Shell redirect as IPC." |
| 2 | **Node identity is a path-string that two independent passes must derive identically.** IDs are `{full_path_with_underscores}_{symbol}`; changed twice (#1504), each change orphaning old graphs. AST `make_id` and the LLM's prose-spec'd id must match byte-for-byte. | ghost/duplicate nodes, orphaned graphs, `extract --force` required, ~6 layered id-remap backstops (#1529/#2231/#2262/#2243/#2538), Turkish `İ` / Greek `U+0345` casefold desync (#2614). |
| 3 | **Incremental update is manifest-diffing with many footguns.** | `dedup=True` disables the shrink guard; `_infer_merge_root` guesses wrong → every prune silently no-ops → ghosts forever; misattributed semantic fragment permanently deletes a file's nodes; `kind` mismatch wedges the graph; NFC/NFD + absolute/relative key drift. Shrink-guard + empty-graph-guard exist *because* the build is not idempotent. |
| 4 | **Retrieval is lexical.** `query` tokenizes, casefold substring/prefix/exact match against `label` + `source_file` + id, IDF-weights, seeds ≤3 nodes, BFS/DFS depth 2, renders to a ~2000-token budget. | `"authentication"` vs `login`/`Guardian` → **zero hits**; the skill works around it with an LLM "constrained query expansion" step. No stemming, no synonyms, no embeddings, no fuzzy, no rerank. One exact hit on a common word (`home`, `data`, `get`) dominates ranking 1000:1. |

### What is genuinely good — **keep all of it**

- **Honest provenance.** Every edge tagged `EXTRACTED` / `INFERRED` / `AMBIGUOUS` with a `source_location`. Rare and valuable.
- **AST + semantic split.** Deterministic structure from AST; LLM only for what AST can't see.
- **Persistent graph across sessions** + incremental intent.
- **Community detection** surfacing non-obvious cross-module links (god nodes, surprising connections).
- **Multi-output** — one graph → viz, RAG JSON, human report.
- **Provenance-first report** with god nodes and suggested questions as a "first read" of an unknown repo.
- **`graph.json` as the interchange format** — never break it.

---

## Token economics — why a graph beats raw context

The product claim is *fewer tokens per completed task* for an AI coding agent.
The core principle: **the graph ships conclusions, not evidence.** An agent burns
tokens re-deriving the same structural facts from raw source every session;
codegraph derives them once offline, stores the compressed result, and serves a
bounded slice on demand.

### Mechanisms

1. **Retrieval replaces dumping.** `query "<q>"` returns a subgraph capped at a
   token budget (default ~2000). The offline pipeline already did the "which
   files matter" work. Without it the agent greps -> 8 hits -> opens 5 files at
   3–8k tokens each -> 20–30k tokens just to *locate* the answer.
2. **An edge is ~15 tokens; the fact behind it is hundreds.**
   `EDGE handle_request --calls--> login [EXTRACTED]` is ~15 tokens. For the
   agent to know that from source it must read both function bodies and trace the
   call. The graph stores the edge, not the bodies.
3. **`affected` collapses a multi-call exploration into one.** "What breaks if I
   change `login()`" without the graph = grep, open every caller, read
   surrounding context, still miss indirect callers — thousands of tokens over
   4–5 round trips. With the graph: one call, bounded `symbol @ file:line` list.
4. **Line-precise locations enable targeted reads.** Every node carries
   `source_file:L120-L145`. The agent reads 25 lines, not the 800-line file. The
   graph is the index that makes a surgical read possible.
5. **Provenance lets the agent skip verification.** `EXTRACTED` = structural
   fact, trust it. Only `INFERRED` / `AMBIGUOUS` justify opening the file. The
   agent doesn't re-confirm what the AST already proved.
6. **`GRAPH_REPORT.md` replaces the orientation phase.** God nodes + communities
   + entry points in a few hundred tokens = the repo-tree-walk + README + "open
   some likely files" the agent otherwise does at session start.
7. **Query expansion is local, not an LLM round trip.** The FTS + rapidfuzz
   vocab step (`authentication` -> `authenticate`, `login`) runs in-process.
   graphify makes the host LLM brainstorm synonyms against a vocab dump before
   searching — a full round trip codegraph removes.
8. **Persistent + incremental across sessions.** Built once, `update` re-parses
   only content-changed files. Every future question amortizes against that. The
   raw-context approach re-pays the full read cost every session because context
   windows don't persist.
9. **Deterministic output = cache-friendly.** `graph.json` and the report are
   byte-stable turn to turn, so graph context pinned in a prompt keeps the
   prompt cache warm. Raw file dumps churn and invalidate it.

### The round-trip multiplier

In an agentic loop every tool call re-sends the entire growing conversation.
Cutting exploratory calls from 6 -> 2 saves not just those 4 calls' output but
4× re-transmission of everything before them. Judge cost per *completed task*,
not per call.

### Worked estimate — "how does X work" on a medium repo

| | raw context | codegraph |
|---|---|---|
| tool calls to locate | grep + ~5 file reads | 1 `query` |
| input tokens | ~20–30k | ~2k subgraph + ~1k targeted read |
| round trips | 4–6 | 1–2 |

Roughly **5–8× fewer tokens** on that shape, plus far fewer round trips.

### Honest caveats

- Building the graph has a one-time cost (the semantic/LLM pass; the AST tier is
  free).
- A stale graph misleads — `update` is cheap but must run (the skill checks
  freshness against `built_at_commit`).
- Retrieval recall isn't perfect; a missed seed sends the agent back to grep, so
  this is a floor on cost, not a ceiling.
- The token win is real only if the agent is *told* to query first — hence the
  `/codegraph` skill's step 1 and the MCP server: the graph has to be the
  agent's default first move, not an afterthought.

### What this implies for the build

- **Keep the `query` render terse.** Every token in the subgraph output competes
  with the agent's own reasoning budget. `NODE`/`EDGE` lines, no prose, no
  redundant fields. (Already the case; guard it.)
- **`token_budget` is a first-class knob**, honored exactly, with the header
  stating how many nodes were cut so the agent knows to narrow the query rather
  than re-ask broadly.
- **Every node must have a precise `source_location`** — a node without a line
  range forces a whole-file read and forfeits mechanism #4.
- **`affected` and `explain` must return `file:line` for every hit**, so the
  answer is directly actionable without a follow-up search.
- **Ship a `context <symbol>` command** (Phase 2): emit exactly the code slices
  the agent needs for a task — the target symbol's body plus its direct
  callers/callees' signatures — as one bounded blob, so the agent makes zero
  exploratory reads.

---

## 1. Agreed decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Built in Python.** Ships as `pip install codegraph`, same console-script command style, same `/codegraph` agent skill. Reuse `tree-sitter` bindings, `rapidfuzz`, `networkx` (for algorithms only). | Real drop-in. No rewrite of the tree-sitter extractor investment. |
| 2 | **SQLite is the real store.** All graph state lives in `graphify-out/codegraph.db`. `graph.json` / `GRAPH_REPORT.md` / `graph.html` are **rendered outputs** re-emitted from the DB, byte-compatible with graphify's schema. | Fixes most of issue-cluster #1 and #3: transactional writes, no sidecar IPC, no shrink-guard-as-safety-net, deterministic per-file replacement, real incremental. |
| 3 | **Real retrieval.** Proper word-splitting + stemming + typo tolerance (trigram/edit-distance) + optional embeddings, then graph expansion, then rerank. `query "authentication"` must find `login` without an LLM pre-expansion step. | Fixes issue-cluster #4. Removes the biggest reason the skill layer is complex. |
| 4 | **Cover graphify's whole command surface in the design, build in phases.** Core (`extract`/`update`/`query`/`explain`/`path`/`affected`/`god-nodes`/`export html json report`) first; more languages second; the long tail of exporters (neo4j, falkordb, obsidian, wiki, graphml, svg) and ingestion (video, arXiv, Twitter, PR dashboards) as plugins after. | graphify's `install.py` is 2,355 lines and it exports to 6 graph DBs — enormous surface for a tool whose core value is "code → queryable graph." |

### The architectural inversion (summary table)

| Area | graphify | codegraph |
|---|---|---|
| Orchestration | LLM + 750-line runbook + sidecar files | Single package, real in-process pipeline, SQLite state, one thin agent skill |
| Store | `graph.json` reloaded into NetworkX every query | SQLite (`nodes`, `edges`, `fts`, `vec`, `manifest`, `communities`, `meta`); NetworkX built on demand for graph algorithms |
| Extraction | 31 hand-rolled tree-sitter extractors + fuzzy resolvers | Same tree-sitter extractors (ported), **optionally** SCIP / stack-graphs / LSP where an indexer exists; tree-sitter is the fallback tier |
| Symbol resolution | name-key + path proximity, "single-definition god-node guard" drops all ambiguity | keep the heuristic resolvers, but when a scope/type-aware indexer is present prefer its edges (tagged `EXTRACTED`, evidence = indexer) |
| Graph model | undirected multigraph, `--directed` opt-in and historically leaky | **directed + typed edges first-class**; `calls`/`imports`/`imports_from`/`contains`/`method`/`inherits`/`implements`/`references` always carry true direction |
| Node identity | path-string id both passes must re-derive | **DB-assigned integer PK**; the `{stem}_{entity}` string becomes a *non-authoritative* `slug` column (kept for `graph.json` compatibility); LLM never mints an id |
| Incremental | manifest diffing, shrink guard, many footguns | file content-hash → delete-and-reinsert that file's rows in one transaction; deterministic; no shrink guard needed |
| Semantic layer | strict-JSON LLM with byte-exact id-matching burden | LLM annotates **existing** nodes by (file, symbol, line) match → attaches `rationale`, `concepts`, `AMBIGUOUS` edges; never emits a structural node id |
| Retrieval | substring + IDF + BFS-2 | FTS5 + stemming + trigram fuzzy (+ optional vectors) → candidate seeds → typed graph expansion → rerank; `affected` = real reverse-`calls` traversal |
| Scope | everything in one binary | core + query + one viz; exporters and exotic ingestion are entry-point plugins |

---

## 2. Storage model (`graphify-out/codegraph.db`)

SQLite, WAL mode, one file. Schema (`schema_version` in `meta`):

```
meta(key TEXT PRIMARY KEY, value TEXT)          -- schema_version, root, directed,
                                                --   built_at_commit, prompt_fingerprint,
                                                --   codegraph_version, tokenizer, …

files(path TEXT PRIMARY KEY,                     -- forward-slash, root-relative, NFC
      abs_identity TEXT,                         -- canonical absolute posix, for prune matching
      file_type TEXT,                            -- code|document|paper|image|video
      lang TEXT,
      content_sha256 TEXT,
      ast_hash TEXT, semantic_hash TEXT,         -- "" = needs (re)extraction
      mtime REAL, seen REAL,
      status TEXT)                               -- present|excluded|deleted

nodes(id INTEGER PRIMARY KEY,
      slug TEXT,                                 -- graphify-compatible {stem}_{entity}; NOT unique key
      label TEXT, norm_label TEXT,
      file_type TEXT,
      source_file TEXT REFERENCES files(path),
      source_location TEXT,                      -- "L12-L30" | "L1" | NULL
      definition_file TEXT, definition_location TEXT,
      kind TEXT,                                 -- function|method|class|interface|namespace|module|file|concept|…
      origin TEXT,                               -- ast|semantic|stub
      rationale TEXT,                            -- LLM, nullable
      community INTEGER, community_name TEXT,
      degree INTEGER,                            -- denormalised, maintained on write
      extra JSON)                                -- author, contributor, captured_at, source_url, metadata, verification

edges(id INTEGER PRIMARY KEY,
      src INTEGER REFERENCES nodes(id),          -- ACTOR (true direction, always)
      dst INTEGER REFERENCES nodes(id),          -- ACTED-UPON
      relation TEXT,
      confidence TEXT,                           -- EXTRACTED|INFERRED|AMBIGUOUS
      confidence_score REAL,                     -- discrete {1.0 | 0.55..0.95 | 0.2}
      context TEXT,                              -- call|import|field|parameter_type|return_type|generic_arg|attribute
      source_file TEXT, source_location TEXT,    -- the relation *site*
      weight REAL DEFAULT 1.0,
      evidence TEXT,                             -- import|scip|stack-graphs|same-file|proximity|llm
      deferred INT, type_only INT,
      UNIQUE(src, dst, relation, source_file, source_location))

hyperedges(id INTEGER PRIMARY KEY, slug TEXT, label TEXT,
           relation TEXT, confidence TEXT, confidence_score REAL, source_file TEXT)
hyperedge_members(hyperedge_id INTEGER, node_id INTEGER)

nodes_fts USING fts5(label, slug, source_file, rationale,
                     content=nodes, tokenize='porter unicode61 remove_diacritics 2')
node_trigrams(trigram TEXT, node_id INTEGER)     -- fuzzy candidate index
node_vec(node_id INTEGER PRIMARY KEY, embedding BLOB)  -- optional; sqlite-vec or a numpy sidecar

communities(id INTEGER PRIMARY KEY, label TEXT, cohesion REAL, member_sig TEXT)
queries(ts, kind, question, corpus, nodes_returned, duration_ms, outcome, correction)  -- was querylog + memory/
```

Key consequences:

- **Identity is the integer PK.** `slug` is emitted into `graph.json` as the node `id` for
  backward compatibility, computed once by one code path (`ids.make_slug`), never round-tripped
  as an authority. Collisions on `slug` are allowed in the DB and disambiguated only at
  `graph.json` render time (append `#2`, `#3`), so a slug collision can never merge two real
  symbols or orphan a graph.
- **Direction is a column,** not an out-of-band `_src`/`_tgt` attribute pair that NetworkX
  round-trips can flip (graphify #760, #563, #1061).
- **Incremental update = one transaction:** `DELETE FROM nodes WHERE source_file=? ; DELETE
  FROM edges WHERE source_file=? ; <re-insert> ; UPDATE files SET ast_hash=…`. No shrink
  guard, no `_infer_merge_root`, no prune-set derivation, no ghost merge. A crash mid-write
  rolls back. (graphify's entire `build_merge` / `merge_raw_extraction` / `_build_prune_sets`
  / `_derive_prune_root` / 5 shrink guards collapse to this.)
- **`graph.json` is a `SELECT` + serializer**, always consistent, always the same bytes for
  the same DB (canonical key order: `id`,`label` then sorted; `links` array; sorted by
  canonical JSON string — matches `export.to_json`).

---

## 3. Pipeline

```
codegraph extract <path>
  │
  ├─ 1. detect   ── walk, .gitignore/.codegraphignore (multi-encoding read, NFC),
  │                 sensitive-file skip (.ssh/.aws/.env/id_rsa/… — port graphify's 3-stage
  │                 _is_sensitive), noise-dir prune, classify code|doc|paper|image|video
  │                 → upsert files table
  │
  ├─ 2. extract-ast (deterministic, no LLM, parallel by file)
  │       per file:  tree-sitter parse → nodes + edges + raw_calls
  │       global:    import resolution, cross-file call resolution, decl/def merge,
  │                   stub rewire, language resolvers
  │       identity:  DB PK assigned on insert; slug computed; NO id-remap chain
  │       optional:  if a SCIP index / stack-graphs / LSP is available for a language,
  │                   ingest its symbols+refs as EXTRACTED edges (evidence=scip)
  │       → upsert nodes/edges for changed files only, in one transaction per file
  │
  ├─ 3. extract-semantic (LLM, only files with semantic_hash="")
  │       input:  file text + the AST nodes already in the DB for that file
  │       ask:    "annotate these nodes: rationale, concepts; add AMBIGUOUS edges you
  │                can see that AST missed; propose hyperedges"
  │       output: keyed by (source_file, label, line) — resolver matches to existing PKs
  │       LLM never returns a node id.  Unmatched annotations are dropped + logged.
  │       backend: gemini | claude | claude-cli (Pro/Max plan) | openai | deepseek |
  │                ollama | subagent(host) — same BACKENDS table as graphify
  │
  ├─ 4. cluster   ── build NetworkX Graph from DB → Leiden (graspologic_native, direct
  │                  call, no graspologic import) → Louvain fallback → canonical sorted
  │                  edge order for seed-stability → hub exclusion → oversized split →
  │                  cohesion re-split → stable community ids (remap to previous)
  │                  → write communities table + nodes.community
  │
  ├─ 5. analyze   ── god nodes, surprising connections, import cycles, suggested questions
  │
  └─ 6. render    ── graph.json + GRAPH_REPORT.md + graph.html  (all from DB)
```

`codegraph update <path>` = steps 1, 2, 4, 5, 6 (no LLM; blanks `semantic_hash` for
content-changed files so a later `extract` repopulates).
`codegraph watch <path>` = `update` on a debounced file-watcher.

---

## 4. Retrieval (`query`, `explain`, `path`, `affected`)

### `query "<question>"`

1. **Tokenize** — Unicode word-split, keep short identifiers (`os`, `io`, `db` — graphify
   drops all-lowercase ≤2), NFKD fold, optional `jieba` for CJK.
2. **Term expansion (no LLM):**
   - Porter stemming via FTS5 (`authentication` ↔ `authenticate` ↔ `authenticated`).
   - Trigram + Damerau-Levenshtein fuzzy against `node_trigrams` for typos (`autentication`).
   - Optional synonym hop through embeddings: nearest label vectors to the query
     (`authentication` → `login`, `session`, `credential`) when `node_vec` is populated.
3. **Score candidates** — FTS5 BM25 over `label`/`slug`/`source_file`/`rationale`, plus the
   graphify tiered bonuses (exact/prefix/substring) but **re-weighted so one exact hit on a
   common word cannot dominate** (cap the exact bonus at `k × best_substring`, not 1000×),
   plus coverage `(matched/n_terms)`.
4. **Seed selection** — up to `max_k` (default **8**, graphify uses 3), one guaranteed seed
   per matched term, label-dedup homonyms, relational-verb demotion.
5. **Graph expansion** — directed BFS/DFS depth 2–3 from seeds over the typed edge set;
   hub-skip threshold `max(50, p99)` but seeds always expand; `_complete_induced_edges` for
   seed↔seed / hub↔hub.
6. **Rerank** — survivors ranked by `(relevance_to_query, hop_distance, -degree)`; render to
   a token budget (consistent chars/token, unlike graphify's 3× vs 4× split).

### `affected "<symbol>"` — the "impact of change" query

Real reverse traversal over `calls` / `references` / `imports_from` / `implements` edges
(direction is authoritative in the DB), depth-N, grouped by file and community. This is the
query graphify's BFS-2-from-lexical-seed approximates badly.

### `path "<a>" "<b>"` / `explain "<node>"`

One node-resolution function (graphify has two that disagree — `_score_nodes` vs
`_find_node` tiers). Tiers: source-exact → exact → prefix → fuzzy. Ambiguity reported when
the winning tier spans >1 file.

### Feedback loop (`save-result` / `reflect`)

Keep it, but it's `INSERT INTO queries` + a `SELECT`-based aggregation with time-decay
(`0.5 ** age/half_life`), not markdown files parsed by a hand-rolled YAML subset. Surfaces
"preferred sources" / "known dead ends" into `GRAPH_REPORT.md` and as a `learning=` suffix
on `explain` output.

---

## 5. `graph.json` compatibility contract

Emitted from the DB, must satisfy every reader graphify has:

- Top-level: `directed` (bool), `multigraph` (bool), `graph` (dict, may hold `hyperedges`),
  `nodes` (list), **`links`** (list — key is `links`, readers also accept `edges`),
  `hyperedges` (list, top-level), `built_at_commit` (string, only if resolvable).
- Node: `id` (= slug), `label` first; then all keys sorted. Always write `community`
  (int|null), `norm_label`. `community_name` only when labels exist and cid≠null.
  Carry `file_type`, `source_file` (relative POSIX), `source_location`, `rationale`,
  `definition_file`/`definition_location`, `verification`.
- Link: `source`, `target`, `relation` first, then sorted. `source`/`target` carry **true
  caller→callee direction**. `confidence` always; `confidence_score` always (fill from
  `{EXTRACTED:1.0, INFERRED:0.55, AMBIGUOUS:0.2}` if absent). `source_file`,
  `source_location`, `weight`, `context`, `deferred`, `type_only`.
- Determinism: identity keys first, remaining `sorted()`; `nodes` and `links` sorted by
  `json.dumps(item, sort_keys=True, separators=(",",":"))`; atomic write, `indent=2`.
- A `codegraph`-written `graph.json` opens unchanged in graphify's HTML viewer, MCP server,
  and all its exporters.

---

## 6. MCP server

`python -m codegraph.serve <db-or-graph.json>` — stdio + Streamable HTTP, same tool names
and schemas as graphify so existing agent configs keep working:

`query_graph`, `get_node`, `get_neighbors`, `get_community`, `god_nodes`, `graph_stats`,
`shortest_path` (+ the PR tools `list_prs`/`get_pr_impact`/`triage_prs` as an optional
plugin). Reads straight from SQLite (no "reload graph.json into NetworkX per query" — a
`GraphContextCache` keyed on db mtime, hot-reload on change).

---

## 7. The `/codegraph` agent skill

graphify's SKILL.md is ~750 lines because the LLM *is* the orchestrator. codegraph's skill
is thin because the package orchestrates itself. It should be roughly:

```
/codegraph <path-or-question>

1. Is there a graphify-out/codegraph.db under <path>?
     no  → run `codegraph extract <path> --backend <detected>` ; report the summary
     yes → is it stale (git HEAD moved / files changed)?
              yes → `codegraph update <path>` (+ `extract` if semantic gaps)
2. If the input was a question → `codegraph query "<question>"` and answer from the result,
   then `codegraph save-result` with the outcome.
3. For "what breaks if I change X" → `codegraph affected "X"`.
```

The LLM's only real jobs: pick a backend, decide extract-vs-update, phrase the answer, file
the feedback. Everything stateful is a DB transaction inside the package. No heredocs, no
sidecar files, no encoding hazards in the IPC path (there is no IPC path).

Semantic extraction backends still include `subagent` (host LLM does the annotation) and
`claude-cli` (user's Pro/Max plan) so it runs with no API key, same as graphify.

---

## 8. Things graphify does that codegraph must NOT regress

From the deep-dive research — these are load-bearing, keep the behavior:

- **Sensitive-file skipping** — 3-stage `_is_sensitive` (`.ssh`/`.gnupg`/`.aws`/`.gcloud`
  dirs; `.env`/`.pem`/`id_rsa`/`.netrc`/… filename regex with `.env.example` exemption;
  generic `secret`/`password`/`token`/`service_account` keyword when load-bearing). Skipped
  files never reach the LLM.
- **Ignore-file encoding** — read `.gitignore`/`.codegraphignore` UTF-8-sig → UTF-16-BOM →
  locale → latin-1, warn once; NFC-normalize both sides of every fnmatch (macOS NFD). A
  Windows-ANSI `.gitignore` with `Orçamento/` must still match.
- **NFC everywhere** — every path membership test (manifest keys, `source_file`, prune
  matching) NFC-normalizes both sides.
- **Windows paths** — `\\?\` long-path prefix for I/O, stripped for keys; `os.replace` →
  `PermissionError` fallback to copy-then-delete (AV/locked handle).
- **Leiden ANSI output** — `graspologic` emits ANSI escapes that corrupt the PowerShell 5.1
  scroll buffer; suppress stdout/stderr around the partition call.
- **Cross-language phantom edges** — don't create `calls`/`imports` edges across language
  families without evidence (graphify bans them outright; codegraph keeps the ban for
  INFERRED, allows EXTRACTED when an indexer or explicit FFI/codegen marker proves it).
- **Discrete confidence scores** — INFERRED ∈ {0.55, 0.65, 0.75, 0.85, 0.95}; models
  collapse a continuous range to bimodal 0.5/0.85 (documented in graphify's extraction-spec).
- **Determinism** — sorted iteration everywhere a dict/set feeds output; fixed seeds;
  canonical edge ordering before clustering (measured: 70 vs 69 communities across
  `PYTHONHASHSEED` values without it).
- **`built_at_commit`** in the report + a freshness nudge when git HEAD has moved.
- **Backup-on-write** — before overwriting a graph that cost real LLM tokens, copy the
  artifacts to `graphify-out/<YYYY-MM-DD>/` (graphify's `backup_if_protected`).

---

## 9. Phased build

### Phase 1 — core (the 80%) — ✅ DONE (2026-09-07)
- ✅ `codegraph/db.py` — SQLite schema, transactional `replace_file` (delete+reinsert
  one file's rows in one tx), `resolve_calls` global pass, contentless FTS5 + trigrams.
- ✅ `codegraph/detect.py` — walker, 3-stage sensitive-skip, multi-encoding `.gitignore`
  (NFC fnmatch), code/doc/paper/image/video classification.
- ✅ `codegraph/ids.py` — `make_slug` one code path, NFKC-casefold fixpoint, `file_stem`.
  Slug is non-authoritative (DB integer PK is identity); collisions disambiguated at
  `graph.json` render time.
- ✅ `codegraph/extract/` — generic tree-sitter engine (`engine.py`) + `LangConfig`
  (`langs.py`) for **Python, JavaScript, TypeScript, Go, Java, C, C++, Ruby, C#, Rust**.
  Same-file calls resolved at parse time; cross-file / inheritance / imports emitted as
  `raw_refs` for the global resolver (single-definition god-node guard).
- ✅ `codegraph/llm.py` + `semantic.py` — annotate-existing-nodes pass. Backends:
  `anthropic`, `openai`, `gemini`, `deepseek`, `ollama` (urllib, no dep), `claude-cli`
  (user's plan, no key), `mock` (tests), `none`. LLM never returns a node id — every
  annotation keyed by `(label, line)`, unmatched dropped and counted.
- ✅ `codegraph/cluster.py` — Louvain (graspologic Leiden if installed), canonical sorted
  edge order (seed-stable), hub exclusion, isolates, size-desc stable community ids.
- ✅ `codegraph/analyze.py` — god nodes, surprising connections, import cycles.
- ✅ `codegraph/query.py` — FTS5 porter stem + rapidfuzz local vocab expansion (the step
  graphify outsources to the host LLM) + directed graph expansion + rerank; `explain`,
  `path`, `affected` (real reverse-call traversal), one node-resolution function.
- ✅ `codegraph/reflect.py` — `save-result` / `reflect` over the `queries` table with
  exponential time-decay → preferred sources / dead ends, surfaced in report + `explain`.
- ✅ `codegraph/render/` — `graph_json.py` (compat contract §5, deterministic, atomic),
  `report.py` (GRAPH_REPORT.md), `html.py` (self-contained vanilla-JS force view).
- ✅ `codegraph/cli.py` — `extract update watch query explain path affected god-nodes
  save-result reflect export(json|report|html|all) stats serve`.
- ✅ `codegraph/serve.py` — MCP stdio (line-delimited JSON-RPC 2.0, no `mcp` dep),
  graphify-compatible tool names (`query_graph`, `get_node`, `get_neighbors`,
  `god_nodes`, `graph_stats`, `shortest_path`, + `affected`).
- ✅ The `/codegraph` skill — `~/.claude/skills/codegraph/SKILL.md`.
- ✅ 23 tests (ids, detect, pipeline, semantic, reflect+serve, golden). Golden test
  asserts: valid provenance on every edge, deterministic `graph.json`, no dangling
  links, cross-file + cross-language `calls` resolution, incremental update isolates
  the changed file (same PKs for untouched files), `affected` crosses files.
  Stress-tested on graphify's own 66k-LoC source (86 files, ~1750 nodes, ~15s).

**Phase 1 gaps** — ✅ closed in Phase 2:
- ✅ MCP HTTP transport — `codegraph serve --http` (non-streaming Streamable-HTTP subset).
- ✅ `diagnose` command — `codegraph/diagnose.py`, 8 health checks (parse coverage,
  xref resolution, community balance, provenance, slug collisions, referential
  integrity, semantic coverage, freshness).
- ✅ `analyze` "suggested questions" generator — seeded from god nodes + entry
  points + cohesive communities; rendered in `GRAPH_REPORT.md`.
- ⚠️ Retrieval ranking — still uses the rapidfuzz vocab-expansion heuristic; the
  opt-in embedding tier (below) is the real fix when a model is available.
- ⚠️ `claude-cli` backend nested-session sandbox `api_error` — unchanged (env limit).

### Phase 2 — parity — ✅ DONE (2026-09-07)
- ✅ **`codegraph context <symbol>`** (`codegraph/context.py`) — target body +
  caller/callee signatures + `file:line`, bounded. CLI + MCP `get_context`.
- ✅ More languages — **Kotlin, Swift, PHP, Scala, Lua, Bash** added (16 total).
  `LangConfig.pack_name` + a `tree_sitter_language_pack` fallback in
  `get_language`, so further languages are a config entry, not a new dep.
  Swift/overlap-grammar double-captures deduped by span in the engine.
- ✅ Embeddings (`node_vec` + `vec_cache`) — opt-in `codegraph embed`; OpenAI-compatible
  `/embeddings` backend (openai|gemini|ollama) + a dependency-free `hash` fallback +
  a test mock hook. `vec_cache` is keyed by text-hash so re-`extract` (new PKs)
  costs zero API calls. `query` folds nearest-neighbour hits into the ranking.
- ✅ `merge-graphs` (`codegraph/merge.py`) — `{name}:` slug / `{name}/` path prefix,
  re-cluster + re-analyse. `global {list,add,remove}` (`codegraph/registry.py`,
  `~/.codegraph/registry.json`) + `query --all` cross-repo fan-out.
- ✅ Incremental cache parity — `check-update` (git HEAD drift + per-file sha diff,
  no rebuild), semantic **prompt fingerprint** in `meta` (prompt/model change
  re-runs the annotation pass). `--force` already existed.
- ⏳ SCIP / stack-graphs / LSP ingestion tier — still deferred (needs external
  indexers on PATH; tree-sitter tier covers the common case).

### Phase 3 — the long tail — ✅ DONE (2026-09-07)
- ✅ Exporters (`codegraph/export.py`, all pure DB serializers, no live service):
  `graphml`, `gexf`, `dot` (Graphviz→svg), `cypher` (Neo4j/FalkorDB/Memgraph
  import script), `csv`, `jsonl` (GraphRAG), `mermaid`, `obsidian` (Markdown vault),
  `tree`. `export all` writes every one into `graphify-out/exports/`.
- ✅ Document tier (`codegraph/extract/docs.py`) — Markdown / reST / AsciiDoc /
  setext headings → `section` nodes with `contains` nesting; on by default,
  `--no-docs` to skip. A design note is now reachable by `query` like a function.
- ✅ `install` — writes/merges `.mcp.json` for Claude Code (+ prints the
  `claude mcp add` one-liner). Not 2,355 lines.
- ✅ PR dashboard (`codegraph/prs.py`, `codegraph prs [N]`) — `gh`-backed;
  maps a PR's changed files to nodes, runs the `affected` traversal, ranks open
  PRs by downstream blast radius. MCP `list_prs` / `get_pr_impact`. Degrades
  cleanly when `gh` is absent.
- ✅ `clone` — copy a graph DB to a new root, rewrite `meta.root`, re-render.
- ✅ Remaining languages — **R, Julia, Elixir, PowerShell** added (**20 total**).
  The rest (Zig, Objective-C, Dart, Haskell, OCaml, Fortran, SQL, Terraform,
  Vue/Svelte/Astro, …) are now a `LangConfig` entry each against the
  language-pack fallback — added on demand.

### Phase 4 — external-dependency tier — ✅ DONE (2026-09-07)
- ✅ **SCIP ingestion tier** (`codegraph/scip.py`) — consumes `scip print --json`
  (no protobuf dep), maps occurrences to nodes by enclosing span, emits
  `EXTRACTED` `evidence='scip'` edges that disambiguate overloaded/shadowed names
  the single-definition guard can't. Auto-detected (`*.scip` + `scip` on PATH);
  `extract --scip PATH` / `--no-scip`. Records `meta.resolver_tier`.
- ✅ **Streamable-HTTP + SSE** — `serve --http` now content-negotiates: POST →
  `application/json` or `text/event-stream`; `GET` opens the server→client SSE
  channel with keep-alives; `DELETE` ends a session; `Mcp-Session-Id` minted on
  `initialize`. MCP 2025-03-26 shape.
- ✅ **git merge-driver** (`codegraph/gitmerge.py`, `codegraph merge-driver`,
  `install --git`) — `graphify-out/**` is regenerable: the driver keeps "ours",
  drops a `.needs-rebuild` sentinel, exits 0. Real 3-way-merge conflict test.
- ✅ **`codegraph add <source>`** (`codegraph/ingest.py`) — URL (HTML→text) and
  arXiv (Atom API) in-process; PDF/Office/audio delegate to `pdftotext` /
  `pandoc` / `whisper` when present, clear error otherwise. Writes
  `graphify-out/sources/<slug>.md`, indexes headings as `section` nodes, never
  pruned by `update`.
- ✅ **Live graph-DB load** — `export cypher --run` pushes straight into Neo4j /
  FalkorDB / Memgraph via the `neo4j` driver or `cypher-shell`; the `.cypher`
  and `csv` (for `neo4j-admin import`) scripts remain for manual loading.

### Phase 5 — the last mile — ✅ DONE (2026-09-07)
- ✅ **LSP resolver tier** (`codegraph/lsp.py`) — starts the language server for
  each language present (`pylsp` / `gopls` / `rust-analyzer` / `clangd` /
  `typescript-language-server` / …), asks `textDocument/references` per
  definition, turns each reference site into an `EXTRACTED` `evidence='lsp'` edge.
  Opt-in (`extract --lsp`), auto-detects the binary, no-ops per-language when
  absent. Pure JSON-RPC framing, no dependency. Real integration test against a
  live `pylsp`.
- ✅ **`install --platform`** (`codegraph/installers.py`) — Claude Code,
  Claude Desktop, Cursor, Windsurf, VS Code, Zed. Each knows its config path,
  JSON key (`mcpServers` / `servers` / `context_servers`) and scope; read-merge-write.
- ✅ **Wiki / Notion ingestion** — `codegraph add notion:<id>` (Notion API, block
  tree → Markdown), `codegraph add confluence:<id|url>` (storage-format HTML →
  text). Also recognises `*.notion.so` / `*.atlassian.net/wiki` URLs directly.

### Phase 6 — hardening (PLAN §8 ports) — ✅ DONE (2026-09-07)
- ✅ `codegraph/_util.py`: `suppressed_fds` (fd-level dup2 so native ANSI from the
  Leiden partitioner can't corrupt the PowerShell 5.1 scroll buffer, #19);
  `atomic_replace` (retry + copy-then-delete fallback for AV/editor-locked files);
  `long_path` (`\\?\` prefix past `MAX_PATH`, incl. UNC).
- ✅ Backup-on-write — a graph with LLM rationale or embeddings is copied to
  `graphify-out/<date>/` before a rebuild overwrites it (`_backup_if_protected`).
- ✅ Discrete INFERRED confidence — `{0.55, 0.65, 0.75, 0.85, 0.95}` ladder by
  signal strength in `resolve_calls`; `quantize_confidence()` snaps any
  extractor-supplied INFERRED score to a rung.
- ✅ Fixed the `graspologic` Leiden path (it had always silently fallen back to
  Louvain — `_as_sets` was being fed `.values()` instead of the mapping).
- ✅ Release scaffolding — git repo, `LICENSE` (MIT), `CHANGELOG.md`,
  `.github/workflows/ci.yml` (ubuntu + windows × py3.11/3.12), sdist include list,
  PyPI classifiers/keywords.

### Genuinely out of scope (would be their own packages)
- Twitter/X thread scraping (auth churn, ToS).
- A hosted web dashboard / SaaS.
- `install` for every niche agent platform (the 6 covered are the ones with
  meaningful share; the pattern in `installers.py` is one dict entry each).
- Publishing to PyPI (needs an account / token — the package builds clean).

---

## 10. Open questions — resolved for Phase 1

1. **Embeddings in core or Phase 2?** → **Phase 2.** FTS5 stemming + rapidfuzz vocab
   expansion already fixes the `authentication`/`login` miss; embeddings add a model dep.
2. **Dir name?** → **`graphify-out/` kept**, with a `CODEGRAPH_OUT` override (relative
   name or absolute path), exactly like `GRAPHIFY_OUT`. DB is `graphify-out/codegraph.db`.
3. **Console-script name?** → **`codegraph` only.** The `/codegraph` skill is the
   compatibility layer.
4. **SCIP tier?** → Phase 2; detect indexers on PATH, fall back to tree-sitter, note the
   tier in the report.
5. **`networkx` dependency?** → **kept** (Louvain, cycles, shortest-path, betweenness).
   Not the bottleneck.
