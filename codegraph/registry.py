"""User-level graph registry (``codegraph global …``).

A small JSON file at ``~/.codegraph/registry.json`` mapping a short name to a
project root, so ``codegraph global query "<q>"`` can fan a question across every
registered repo (the cross-repo case graphify's ``global`` covers).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import db_path

REGISTRY_DIR = Path.home() / ".codegraph"
REGISTRY_FILE = REGISTRY_DIR / "registry.json"


def _load() -> dict:
    try:
        return json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    REGISTRY_FILE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def add(name: str, path: Path | str) -> dict:
    root = Path(path).resolve()
    if not db_path(root).exists():
        raise FileNotFoundError(f"no graph at {root} — run `codegraph extract` first")
    data = _load()
    data[name] = {"path": root.as_posix(), "added": time.time()}
    _save(data)
    return data[name]


def remove(name: str) -> bool:
    data = _load()
    if name not in data:
        return False
    del data[name]
    _save(data)
    return True


def entries() -> dict:
    return _load()


def resolve(name: str) -> Path | None:
    e = _load().get(name)
    return Path(e["path"]) if e else None
