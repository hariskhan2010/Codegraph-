# code-graph (npm)

npm wrapper for [**codegraph**](https://pypi.org/project/code-graph/) — turn a
codebase into a persistent, queryable graph (SQLite + tree-sitter) that AI
coding agents query instead of grepping.

```bash
npm install -g code-graph
codegraph setup            # wire it into every AI agent on your machine
```

`postinstall` gets you a working `codegraph` command by, in order:

1. **downloading the standalone binary** for your OS/arch from the matching
   GitHub Release — fully self-contained, no Python; then
2. falling back to **`pipx install code-graph`**, then
3. **`pip install --user code-graph`** (needs Python 3.11+).

The real docs live with the Python package. Common commands:

```bash
codegraph extract .                 # build the graph
codegraph query "how does X work" . # ask it
codegraph affected "someFunc" .     # what breaks if I change this
codegraph setup                     # register with Claude Code / Cursor / Gemini CLI / …
```
