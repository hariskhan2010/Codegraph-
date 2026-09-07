"""Document tier — headings and their nesting become graph nodes.

graphify ingests docs, papers, PDFs and transcripts alongside code; codegraph's
Phase 3 tier covers the common case with no extra dependency: Markdown / reST /
AsciiDoc / plain-text section structure parsed with a small regex, one node per
heading (``kind='section'``), ``contains`` edges by nesting depth. Enough for
``query`` to reach a design note the way it reaches a function.
"""

from __future__ import annotations

import re

from .. import ids
from .engine import FileResult

_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^(=+|-+)\s*$")
_RST = re.compile(r"^([=\-~`:.'\"^_*+#])\1{2,}\s*$")
_ADOC = re.compile(r"^(={1,6})\s+(\S.*)$")


def _headings(text: str, ext: str) -> list[tuple[int, str, int]]:
    """Return ``(level, title, line_no)`` for every heading, 1-based line."""
    lines = text.splitlines()
    out: list[tuple[int, str, int]] = []
    for i, line in enumerate(lines):
        m = _ATX.match(line)
        if m:
            out.append((len(m.group(1)), m.group(2).strip(), i + 1))
            continue
        if ext in (".rst", ".rest"):
            if _RST.match(line) and i > 0 and lines[i - 1].strip() and not _RST.match(lines[i - 1]):
                out.append((1, lines[i - 1].strip(), i))
            continue
        if ext == ".adoc":
            a = _ADOC.match(line)
            if a:
                out.append((len(a.group(1)), a.group(2).strip(), i + 1))
            continue
        # markdown setext
        if _SETEXT.match(line) and i > 0 and lines[i - 1].strip():
            lvl = 1 if line.startswith("=") else 2
            out.append((lvl, lines[i - 1].strip(), i))
    return out


def extract_doc(rel: str, text: str, ext: str) -> FileResult:
    res = FileResult(rel=rel, lang="markdown")
    file_slug = ids.file_slug(rel)
    title = rel.rsplit("/", 1)[-1]
    res.nodes.append({
        "slug": file_slug, "label": title, "kind": "file",
        "file_type": "document", "source_location": "L1",
    })

    heads = _headings(text, ext)
    if not heads:
        return res

    n_lines = text.count("\n") + 1
    stack: list[tuple[int, str]] = []  # (level, slug)
    seen: set[str] = {file_slug}
    for idx, (level, htitle, line_no) in enumerate(heads):
        end = heads[idx + 1][2] - 1 if idx + 1 < len(heads) else n_lines
        slug = ids.symbol_slug(rel, htitle or f"section{line_no}")
        if slug in seen:
            slug = ids.make_slug(slug, f"l{line_no}")
        seen.add(slug)

        while stack and stack[-1][0] >= level:
            stack.pop()
        parent_slug = stack[-1][1] if stack else file_slug

        res.nodes.append({
            "slug": slug, "label": htitle or f"§{line_no}", "kind": "section",
            "file_type": "document",
            "source_location": f"L{line_no}" if line_no == end else f"L{line_no}-L{end}",
        })
        res.edges.append({
            "src_slug": parent_slug, "dst_slug": slug, "relation": "contains",
            "confidence": "EXTRACTED", "confidence_score": 1.0, "context": "contains",
            "source_location": f"L{line_no}", "evidence": "same-file",
        })
        stack.append((level, slug))
    return res
