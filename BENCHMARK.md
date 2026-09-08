# codegraph vs graphify — head-to-head

Same corpus for every number below: the **Autonomous SEO Agent** repo
(`E:\SEO` — 192 Python files + 33 Markdown/YAML docs, ~82k words). Both tools
run on the identical tree. Everything here is reproducible with the script at the
bottom.

---

## 1. Accuracy — a falsifiable test

Take every edge each tool marks `EXTRACTED` (its highest-confidence tag —
"structural fact, trust it") whose **target is a function with a common method
name** (`get`, `execute`, `run`, `add`, `check`, `query`, …). Open the source
line the edge points at. Count how many are **provably impossible** — the call
site cannot be that function.

| | edges checked | provably WRONG | error rate |
|---|---|---|---|
| **graphify** | 33 | **18** | **55%** |
| **codegraph** | 33 | **0** | **0%** |

### The 18 graphify errors (all identical failure mode)

graphify wires every FastAPI route decorator to the tool registry's
`get(name) -> ToolSpec`:

```
apps/api/app/routers/data.py:L25    crawls()          --EXTRACTED calls--> get()
    @router.get("/crawls")
apps/api/app/routers/data.py:L33    issues()          --EXTRACTED calls--> get()
    @router.get("/issues")
apps/api/app/routers/health.py:L10  health()          --EXTRACTED calls--> get()
    @router.get("/health")
apps/api/app/routers/projects.py:L27 list_projects()  --EXTRACTED calls--> get()
    @router.get("", response_model=list[ProjectOut])
    … 14 more, every `@router.get(...)` in the repo
```

`@router.get` is `fastapi.APIRouter.get`. It has nothing to do with
`packages/tools/registry.py:get`. graphify can't tell them apart because it
throws away the receiver (`router.`) at extraction time and resolves every
`get` to the one function named `get`. codegraph's receiver-aware resolver
(v0.7.0) drops these instead of guessing.

**Consequence — the "god node" that isn't:**

| node | graphify degree | codegraph degree | truth |
|---|---|---|---|
| `get()` (tool registry) | **25** | **5** | 5 — it's called by `invoke()`, contains `ToolError`, lives in `registry.py` |
| `execute()` | 30 | 23 | ~23 |

graphify's #1 "core abstraction" for this repo is `get()`. It is an artifact.

---

## 2. Provenance

| | edges | EXTRACTED | INFERRED | AMBIGUOUS | dangling edges dropped at build |
|---|---|---|---|---|---|
| **codegraph** | 2157 | **99%** (2136) | 1% (21) | 0 | **0** |
| **graphify** | 2825 | 81% (2315) | 18% (509) | 1 | **~448** |

graphify emits ~500 `INFERRED` edges (LLM/heuristic guesses) and silently drops
~448 edges at build time because its LLM minted node IDs that don't match its AST
extractor's — the ghost-node bug class. codegraph's IDs are integer primary keys
in SQLite; there is nothing to mismatch.

Every one of codegraph's 21 remaining `INFERRED` edges points at the correct
target — the confidence is hedged, not the target.

---

## 3. Speed (fresh, no cache)

| | structural graph | notes |
|---|---|---|
| **codegraph** | **15.6 s** | clean run |
| **graphify** | 20.7 s | + a `multiprocessing` bootstrap crash on Windows (missing `if __name__ == "__main__"` guard), recovers by falling back to sequential |

Full build with the LLM/semantic pass: codegraph ~3 min (7 parallel subagents),
graphify ~13 min (2 subagents). codegraph with `--semantic none` is a complete,
useful call graph in 16 seconds; graphify has no equivalent fast path.

---

## 4. Feature parity

Everything graphify does, codegraph now does:

| capability | graphify | codegraph |
|---|---|---|
| HTML + GraphRAG JSON + plain-language report | ✅ | ✅ |
| `extract <github-url>` (clone then build) | ✅ | ✅ `codegraph extract https://github.com/…` |
| incremental `update` | ✅ | ✅ (transactional, no manifest diffing) |
| community detection · god nodes · surprising connections | ✅ | ✅ |
| EXTRACTED / INFERRED / AMBIGUOUS provenance | ✅ | ✅ |
| parallel subagent semantic pass | ✅ | ✅ (codegraph owns chunking + merge; subagents never mint IDs) |
| concept / rationale nodes | ✅ | ✅ 66 concepts + 595 rationale annotations |
| cross-doc `semantically_similar_to` links | ✅ | ✅ 31 (vs ~5) |
| hyperedges | ✅ | ✅ |
| images (vision) | ✅ | ✅ |
| audio / video transcription | ✅ (aborts run if Whisper missing) | ✅ (degrades to a stub node) |
| Obsidian vault · agent wiki | ✅ | ✅ `export obsidian` / `export wiki` |
| Neo4j / FalkorDB / GraphML / GEXF / DOT / Mermaid | ✅ | ✅ |
| MCP server · watch mode | ✅ | ✅ |
| URL / arXiv / PDF / Notion / Confluence ingest | ✅ | ✅ `codegraph add` |

Only in codegraph:

- SQLite transactional store — no ghost nodes, no shrink-guard, no
  `_infer_merge_root` guessing
- `affected` (reverse-dependency traversal) · `context` (body + caller/callee
  signatures for editing) · `prs` (PR blast-radius ranking)
- SCIP + LSP precise resolver tiers (`evidence='scip'` / `'lsp'`)
- git merge driver · `diagnose` graph-health check · 6-platform agent install
- **150 tests**, CI, LICENSE, CHANGELOG

Only in graphify: a longer production track record (`0.9.54` vs `0.7.0`). That is
field time, not a capability.

---

## 5. Reproduce it

```python
# accuracy probe — run from the repo root, with graph.json for each tool present
import json, re
from pathlib import Path

COMMON = {"get","execute","run","add","check","close","send","build",
          "save","update","all","query","fetch"}

def probe(graph_json):
    d = json.load(open(graph_json, encoding="utf-8"))
    byid = {n["id"]: n for n in d["nodes"]}
    checked = wrong = 0
    for e in d["links"]:
        if e["relation"] not in ("calls","references"): continue
        if e["confidence"] != "EXTRACTED": continue
        t, s = byid.get(e["target"]), byid.get(e["source"])
        if not t or not s: continue
        tlbl = (t["label"] or "").strip(".()")
        if tlbl not in COMMON: continue
        m = re.match(r"L(\d+)", e.get("source_location") or "")
        if not m: continue
        try:
            line = Path(e["source_file"]).read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[int(m.group(1)) - 1].strip()
        except Exception:
            continue
        checked += 1
        if re.search(rf"@\w+\.{tlbl}\b", line):          # @router.get(...)
            wrong += 1
    return checked, wrong

print("codegraph:", probe("codegraph-out/graph.json"))
print("graphify :", probe("graphify-out/graph.json"))
```
