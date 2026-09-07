"""codegraph — code-to-queryable-graph.

A behavior-compatible successor to graphify: same commands, same ``graph.json``,
same ``/codegraph`` agent workflow, but a real in-process pipeline over a SQLite
store instead of an LLM orchestrating shell steps through sidecar files.
"""

__version__ = "0.5.0"

SCHEMA_VERSION = 1
