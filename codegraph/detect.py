"""Filesystem discovery — walk a project, classify files, skip noise and secrets.

Ported from graphify's ``detect.py`` essentials:

* noise-directory pruning (``node_modules``, ``.git``, ``venv``, build dirs, …)
* ``.gitignore`` / ``.codegraphignore`` with multi-encoding read and NFC fnmatch
  (a Windows-ANSI ``.gitignore`` with ``Orçamento/`` must still match — graphify
  #1810 / #2221)
* 3-stage sensitive-file skipping so credentials never reach the LLM
* extension -> ``code|document|paper|image|video|audio`` classification
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import nfc, rel_posix

# --------------------------------------------------------------------------- #
# classification
# --------------------------------------------------------------------------- #

# suffix -> (file_type, lang)   — lang only meaningful for code
_CODE: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".go": "go",
    ".java": "java",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".cu": "cpp",
    ".rb": "ruby", ".rake": "ruby",
    ".cs": "csharp",
    ".rs": "rust",
    ".kt": "kotlin", ".kts": "kotlin",
    ".swift": "swift",
    ".php": "php", ".php3": "php", ".php4": "php", ".php5": "php", ".phtml": "php",
    ".scala": "scala", ".sc": "scala",
    ".lua": "lua",
    ".sh": "bash", ".bash": "bash",
    ".r": "r",
    ".jl": "julia",
    ".ex": "elixir", ".exs": "elixir",
    ".ps1": "powershell", ".psm1": "powershell",
}
_DOC = {".md", ".mdx", ".rst", ".txt", ".adoc"}
_PAPER = {".tex"}
_IMAGE = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".tiff", ".tif"}
_VIDEO = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
_AUDIO = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma"}

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", ".tox", ".nox", "venv", ".venv", "env",
    ".env", "site-packages", "dist", "build", "target", "out", "bin", "obj",
    ".next", ".nuxt", ".svelte-kit", ".gradle", ".idea", ".vscode",
    "graphify-out", "codegraph-out", ".graphify", ".codegraph", ".worktrees",
    "coverage", ".cache", "vendor",
}
_SKIP_FILES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "go.sum",
    "Cargo.lock", "composer.lock", ".codegraphignore", ".gitignore",
}

# --------------------------------------------------------------------------- #
# sensitive-file detection (graphify's 3 stages)
# --------------------------------------------------------------------------- #

_CREDENTIAL_STORE_DIRS = {".ssh", ".gnupg", ".aws", ".gcloud", ".azure", ".kube"}
_AMBIGUOUS_SENSITIVE_DIRS = {"secrets", ".secrets", "credentials"}

_SENSITIVE_NAME = re.compile(
    r"""(?xi)
    ^\.env($|\.(?!example|sample|template|dist)) |
    ^\.envrc$ |
    \.(pem|key|p12|pfx|cert|crt|der|p8|keystore|jks)$ |
    ^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$ |
    ^secring |
    ^\.(netrc|pgpass|htpasswd|npmrc|pypirc|git-credentials|boto)$
    """
)
_GENERIC_KEYWORD = re.compile(
    r"(?i)(credential|secret|passwd|password|private[_-]?key|api[_-]?key|"
    r"access[_-]?token|service[._-]?account)"
)


def _is_graphable_source(name: str) -> bool:
    return Path(name).suffix.lower() in _CODE


def is_sensitive(rel: str) -> str | None:
    """Return a reason string if ``rel`` (a posix relative path) looks like a
    secret and must be skipped, else ``None``."""
    parts = [p.lower() for p in rel.split("/")]
    name = parts[-1]
    for d in parts[:-1]:
        if d in _CREDENTIAL_STORE_DIRS:
            return f"inside credential store dir '{d}/'"
        if d in _AMBIGUOUS_SENSITIVE_DIRS and not _is_graphable_source(name):
            return f"inside '{d}/' and not source code"
    if _SENSITIVE_NAME.search(name):
        return "filename matches a secret pattern"
    if _GENERIC_KEYWORD.search(name) and not _is_graphable_source(name):
        stem = Path(name).stem
        # load-bearing only: keyword ends the stem, or a short (<=2 word) name
        if _GENERIC_KEYWORD.search(stem.split("_")[-1].split("-")[-1]) or stem.count("_") + stem.count("-") <= 1:
            return "filename contains a secret keyword"
    return None


# --------------------------------------------------------------------------- #
# ignore files
# --------------------------------------------------------------------------- #

_IGNORE_ENCODINGS = ("utf-8-sig", "utf-16", "cp1252", "latin-1")


def _read_ignore(path: Path) -> list[str]:
    raw: bytes = path.read_bytes()
    text: str | None = None
    for enc in _IGNORE_ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        return []
    out: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(nfc(line))
    return out


def _ignored(rel: str, patterns: list[str]) -> bool:
    rel = nfc(rel)
    base = rel.rsplit("/", 1)[-1]
    for pat in patterns:
        neg = pat.startswith("!")
        p = pat[1:] if neg else pat
        p = p.rstrip("/")
        if not p:
            continue
        hit = (
            fnmatch.fnmatch(rel, p)
            or fnmatch.fnmatch(rel, p + "/*")
            or fnmatch.fnmatch(rel, "*/" + p)
            or fnmatch.fnmatch(rel, "*/" + p + "/*")
            or fnmatch.fnmatch(base, p)
        )
        if hit and not neg:
            return True
        if hit and neg:
            return False
    return False


# --------------------------------------------------------------------------- #
# walk
# --------------------------------------------------------------------------- #


@dataclass
class DetectedFile:
    abs_path: Path
    rel: str
    file_type: str
    lang: str | None
    content_sha256: str
    mtime: float


@dataclass
class Detection:
    root: Path
    files: list[DetectedFile] = field(default_factory=list)
    skipped_sensitive: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    unclassified: list[str] = field(default_factory=list)

    @property
    def code(self) -> list[DetectedFile]:
        return [f for f in self.files if f.file_type == "code"]


def _classify(suffix: str) -> tuple[str, str | None] | None:
    s = suffix.lower()
    if s in _CODE:
        return "code", _CODE[s]
    if s in _DOC:
        return "document", None
    if s in _PAPER:
        return "paper", None
    if s in _IMAGE:
        return "image", None
    if s in _VIDEO:
        return "video", None
    if s in _AUDIO:
        return "audio", None
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def detect(root: Path | str, *, extra_ignore: list[str] | None = None) -> Detection:
    root = Path(root).resolve()
    det = Detection(root=root)

    patterns: list[str] = list(extra_ignore or [])
    for name in (".gitignore", ".codegraphignore"):
        p = root / name
        if p.is_file():
            patterns += _read_ignore(p)

    for dirpath, dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        # prune noise dirs in place
        keep = []
        for name in dirnames:
            if name in _SKIP_DIRS or name.startswith("."):
                if name not in (".github",):
                    det.ignored.append(rel_posix(d / name, root) + "/")
                    continue
            rel = rel_posix(d / name, root)
            if _ignored(rel, patterns):
                det.ignored.append(rel + "/")
                continue
            keep.append(name)
        dirnames[:] = sorted(keep)

        for name in sorted(filenames):
            if name in _SKIP_FILES:
                continue
            fp = d / name
            rel = rel_posix(fp, root)
            if _ignored(rel, patterns):
                det.ignored.append(rel)
                continue
            reason = is_sensitive(rel)
            if reason:
                det.skipped_sensitive.append(f"{rel} [{reason}]")
                continue
            cls = _classify(fp.suffix)
            if cls is None:
                det.unclassified.append(rel)
                continue
            ftype, lang = cls
            try:
                st = fp.stat()
                sha = _sha256(fp)
            except OSError:
                continue
            det.files.append(
                DetectedFile(
                    abs_path=fp, rel=rel, file_type=ftype, lang=lang,
                    content_sha256=sha, mtime=st.st_mtime,
                )
            )

    det.files.sort(key=lambda f: f.rel)
    return det
