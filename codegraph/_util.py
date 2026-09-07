r"""Cross-platform I/O and subprocess-noise helpers.

Ports the load-bearing robustness tricks from graphify's issue tail (PLAN §8):

* :func:`suppressed_fds` — silence a native library that writes to fd 1/2
  directly (``graspologic``'s Rust Leiden prints ANSI that corrupts the
  PowerShell 5.1 scroll buffer, graphify #19). A Python-level ``redirect_stdout``
  does not catch native writes; this dup2's the real descriptors.
* :func:`atomic_replace` — ``os.replace`` with a copy-then-delete fallback for
  the Windows "file is being used by another process" case (AV / editor lock).
* :func:`long_path` — the ``\\?\`` prefix so I/O works past ``MAX_PATH`` on
  Windows; a no-op elsewhere and for already-prefixed / relative paths.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def suppressed_fds(*, out: bool = True, err: bool = True):
    """Redirect the process's real stdout/stderr file descriptors to os.devnull
    for the duration of the block, then restore them. Catches writes from native
    extension modules, not just Python-level prints."""
    sys.stdout.flush()
    sys.stderr.flush()
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved: dict[int, int] = {}
    try:
        for fd, want in ((1, out), (2, err)):
            if want:
                saved[fd] = os.dup(fd)
                os.dup2(devnull, fd)
        yield
    finally:
        for fd, old in saved.items():
            os.dup2(old, fd)
            os.close(old)
        os.close(devnull)


def long_path(p: Path | str) -> str:
    r"""Windows: prefix an absolute path with ``\\?\`` so it survives MAX_PATH.
    Returns a plain string usable by ``open`` / ``os.replace``."""
    s = str(p)
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\"):
        return s
    ap = os.path.abspath(s)
    if ap.startswith("\\\\"):          # UNC share
        return "\\\\?\\UNC\\" + ap[2:]
    return "\\\\?\\" + ap


def atomic_replace(src: Path | str, dst: Path | str, *, retries: int = 5) -> None:
    """``os.replace(src, dst)`` with a retry + copy-then-delete fallback for
    ``PermissionError`` (Windows lock held by AV or an editor)."""
    s, d = long_path(src), long_path(dst)
    for attempt in range(retries):
        try:
            os.replace(s, d)
            return
        except PermissionError:
            if attempt < retries - 1:
                time.sleep(0.1 * (attempt + 1))
                continue
            # last resort: copy over the destination, then drop the temp
            try:
                shutil.copyfile(s, d)
                os.unlink(s)
                return
            except OSError:
                raise
