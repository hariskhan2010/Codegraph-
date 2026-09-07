"""Paths and environment configuration.

The output directory is ``codegraph-out`` by default and is overridable with the
``CODEGRAPH_OUT`` environment variable (a bare name, or an absolute path). For
backward compatibility, if no ``codegraph-out`` exists yet but a legacy
``graphify-out/codegraph.db`` does, that directory is reused.
"""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path

OUT_NAME = os.environ.get("CODEGRAPH_OUT", "codegraph-out")
_LEGACY_OUT_NAME = "graphify-out"

DB_NAME = "codegraph.db"
GRAPH_JSON_NAME = "graph.json"
REPORT_NAME = "GRAPH_REPORT.md"
HTML_NAME = "graph.html"


def out_dir(root: Path | str) -> Path:
    """The codegraph output directory for a project root.

    If ``CODEGRAPH_OUT`` is an absolute path it is used verbatim. Otherwise it is
    resolved relative to ``root`` — preferring an existing legacy ``graphify-out``
    graph when the user has not opted into a custom name.
    """
    p = Path(OUT_NAME)
    if p.is_absolute():
        return p
    root = Path(root)
    if "CODEGRAPH_OUT" not in os.environ:
        new = root / OUT_NAME
        legacy = root / _LEGACY_OUT_NAME
        if not new.exists() and (legacy / DB_NAME).exists():
            return legacy
    return root / OUT_NAME


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
