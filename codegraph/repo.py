"""Resolve an ``extract`` source that is a remote git URL to a local checkout.

``codegraph extract https://github.com/owner/repo`` — graphify's
``/graphify https://github.com/owner/repo``. The clone is cached under
``~/.codegraph/repos/`` and refreshed with ``git fetch`` on re-use, so a second
build of the same repo is fast and offline-friendly.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

_GIT_URL = re.compile(
    r"^(?:https?://|git://|ssh://|file://|git@)"
    r"|^[\w.-]+@[\w.-]+:"          # scp-style  git@host:owner/repo
    r"|\.git/?$",
    re.I,
)


def is_git_url(src: str) -> bool:
    return bool(_GIT_URL.search(src.strip()))


def _cache_dir() -> Path:
    d = Path.home() / ".codegraph" / "repos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slug(url: str) -> str:
    s = re.sub(r"^(https?://|git://|ssh://|file://|git@)", "", url.strip())
    s = s.replace(":", "/").removesuffix(".git").strip("/")
    return re.sub(r"[^\w.-]+", "-", s).strip("-")[-80:] or "repo"


def resolve_source(src: str, *, branch: str | None = None,
                   dest: str | Path | None = None) -> Path:
    """Local path unchanged; a git URL -> a cached shallow clone's path."""
    if not is_git_url(src):
        return Path(src)
    if not shutil.which("git"):
        raise RuntimeError("`git` not on PATH — clone the repo yourself and "
                           "point codegraph at the local path")
    target = Path(dest).resolve() if dest else _cache_dir() / _slug(src)

    def _run(args: list[str], cwd: Path | None = None) -> None:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                           text=True, timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()[:400]}")

    if (target / ".git").is_dir():
        _run(["fetch", "--depth", "1", "origin"], target)
        head = branch or subprocess.run(
            ["git", "remote", "show", "origin"], cwd=target,
            capture_output=True, text=True,
        ).stdout
        ref = branch or "HEAD"
        _run(["checkout", "-f", ref if branch else "FETCH_HEAD"], target)
    else:
        args = ["clone", "--depth", "1"]
        if branch:
            args += ["--branch", branch]
        args += [src, str(target)]
        _run(args)
    return target
