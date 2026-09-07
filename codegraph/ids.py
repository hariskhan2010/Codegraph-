"""Slug generation — the graphify-compatible ``{stem}_{entity}`` node id string.

In codegraph the authoritative identity is the SQLite integer primary key. A
slug is a *non-authoritative* label kept only so the rendered ``graph.json`` uses
the same node ``id`` strings graphify would have produced. Slug collisions are
allowed in the database and are disambiguated (``#2``, ``#3`` …) only at
``graph.json`` render time, so a collision can never merge two real symbols or
orphan a graph — unlike graphify, where the slug *is* the identity.

The normalization recipe is ported verbatim from graphify's ``ids.normalize_id``
so an existing graph's ids line up: casefold ∘ NFKC in a bounded fixpoint loop
(the historically fragile part — Turkish ``İ`` #2614, Greek ypogegrammeni), then
collapse non-word runs to ``_``.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath

_NON_WORD = re.compile(r"[^\w]+", re.UNICODE)
_UNDERSCORES = re.compile(r"_+")


def normalize(s: str) -> str:
    """Idempotent, caseless-stable identifier normalization.

    Guarantees (test-enforced): ``normalize(normalize(s)) == normalize(s)`` and
    ``normalize(s) == normalize(s.casefold())``. ``re.UNICODE`` keeps CJK /
    Cyrillic / accented letters so they do not collapse to a single per-file node.
    """
    cur = s
    for _ in range(6):  # bounded fixpoint: casefold and NFKC can each expand
        nxt = unicodedata.normalize("NFKC", cur.casefold())
        if nxt == cur:
            break
        cur = nxt
    cur = _NON_WORD.sub("_", cur)
    cur = _UNDERSCORES.sub("_", cur)
    return cur.strip("_")


def make_slug(*parts: str) -> str:
    """Join ``parts`` with ``_`` and normalize. Empty / falsy parts are dropped;
    leading/trailing ``_`` and ``.`` are stripped from each part first."""
    joined = "_".join(p.strip("_.") for p in parts if p)
    return normalize(joined)


def file_stem(rel_path: str) -> str:
    """Full repo-relative path with the last extension dropped, kept posix.

    ``docs/v1/api/README.md`` -> ``docs/v1/api/README`` (every segment preserved,
    so same-named files in different directories do not collide — graphify #1504).
    The scan root itself -> ``""``.
    """
    p = PurePosixPath(rel_path.replace("\\", "/"))
    if not p.name:
        return ""
    return p.with_suffix("").as_posix()


def file_slug(rel_path: str) -> str:
    """Slug for a file node. ``src/auth/session.py`` -> ``src_auth_session``."""
    return make_slug(file_stem(rel_path))


def symbol_slug(rel_path: str, *entity_parts: str) -> str:
    """Slug for a symbol node: ``{file_stem}_{entity}`` normalized.

    ``src/auth/session.py`` + ``Session`` + ``login`` -> ``src_auth_session_session_login``.
    """
    return make_slug(file_stem(rel_path), *entity_parts)
