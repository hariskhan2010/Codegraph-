"""Paths and environment configuration.

Mirrors graphify's ``GRAPHIFY_OUT`` contract: the output directory name is
``graphify-out`` by default (so existing agent configs and the graphify HTML
viewer keep working) and is overridable with ``CODEGRAPH_OUT``.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

# Default kept as "graphify-out" for drop-in compatibility. Override with CODEGRAPH_OUT.
OUT_NAME = os.environ.get("CODEGRAPH_OUT", "graphify-out")

DB_NAME = "codegraph.db"
GRAPH_JSON_NAME = "graph.json"
REPORT_NAME = "GRAPH_REPORT.md"
HTML_NAME = "graph.html"


def out_dir(root: Path | str) -> Path:
    """The ``graphify-out`` directory for a project root.

    If ``CODEGRAPH_OUT`` is an absolute path it is used verbatim; otherwise it is
    resolved relative to ``root``.
    """
    p = Path(OUT_NAME)
    if p.is_absolute():
        return p
    return Path(root) / OUT_NAME


def db_path(root: Path | str) -> Path:
    return out_dir(root) / DB_NAME


def nfc(s: str) -> str:
    """NFC-normalize a string. Every path membership test must NFC both sides
    (macOS produces NFD; Windows/Linux produce NFC)."""
    return unicodedata.normalize("NFC", s)


def rel_posix(path: Path, root: Path) -> str:
    """Root-relative, forward-slash, NFC path string. Falls back to a resolved
    relativity attempt (symlinked roots), then to the absolute posix form."""
    try:
        r = path.relative_to(root)
    except ValueError:
        try:
            r = path.resolve().relative_to(root.resolve())
        except ValueError:
            return nfc(path.as_posix())
    return nfc(r.as_posix())
