"""git merge driver for the ``codegraph-out/`` artifacts.

The database is the source of truth; ``graph.json`` / ``GRAPH_REPORT.md`` /
``graph.html`` / ``codegraph.db`` are regenerable. When two branches each rebuilt
the graph, a textual 3-way merge of these files is meaningless. This driver keeps
the local ("ours") version, drops a ``.needs-rebuild`` sentinel, and exits 0 so
the merge completes — then ``codegraph update`` (or the ``/codegraph`` skill's
freshness check) regenerates everything cleanly.

Registered by ``codegraph install --git``:

  .gitattributes:  codegraph-out/** merge=codegraph
  git config       merge.codegraph.driver "codegraph merge-driver %O %A %B %P"
"""

from __future__ import annotations

from pathlib import Path

from .config import OUT_NAME

DRIVER_NAME = "codegraph"
_OUT_DIRS = (OUT_NAME, "graphify-out", "codegraph-out")
_ATTR_LINE = f"{OUT_NAME}/** merge=codegraph -text\n"


def merge_driver(base: str, ours: str, theirs: str, path: str) -> int:
    """Called by git as ``codegraph merge-driver %O %A %B %P``.

    git passes ``%A`` as a *temp file* pre-filled with our version and copies it
    back into the worktree afterwards — so leaving it untouched and returning 0
    resolves the conflict in favour of ours. ``%P`` is the in-repo pathname and
    the CWD is the repo root, which is how we locate the output dir.
    """
    marker_dir: Path | None = None
    if path:
        parts = Path(path).parts
        for name in _OUT_DIRS:
            if name in parts:
                marker_dir = Path(*parts[: parts.index(name) + 1])
                break
    if marker_dir is None:
        marker_dir = Path(OUT_NAME)
    try:
        marker_dir.mkdir(parents=True, exist_ok=True)
        (marker_dir / ".needs-rebuild").write_text(
            f"merge kept local {path or '(unknown)'}; run `codegraph update`\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    return 0


def install(root: Path) -> list[str]:
    """Wire the driver into ``root``'s repo. Returns human-readable notes."""
    import subprocess

    notes: list[str] = []
    gitattr = root / ".gitattributes"
    existing = gitattr.read_text(encoding="utf-8") if gitattr.exists() else ""
    if f"{OUT_NAME}/** merge=codegraph" not in existing:
        with gitattr.open("a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(_ATTR_LINE)
        notes.append(f"appended to {gitattr}")
    else:
        notes.append(f"{gitattr} already configured")

    cfg = [
        ("merge.codegraph.name", "codegraph regenerable-artifact merge"),
        ("merge.codegraph.driver", "codegraph merge-driver %O %A %B %P"),
    ]
    for key, val in cfg:
        try:
            subprocess.run(["git", "-C", str(root), "config", key, val],
                           check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as e:
            notes.append(f"could not set {key}: {e}")
            return notes
    notes.append("git config merge.codegraph.* set")
    return notes
