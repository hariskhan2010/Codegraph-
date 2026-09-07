"""``codegraph add <source>`` — pull an external document into the graph.

graphify ingests URLs, arXiv papers, transcripts and Office docs alongside code.
codegraph keeps the same entry point with no heavy dependency: text and HTML are
handled in-process (``urllib`` only); PDF / Office / audio delegate to a CLI
(``pdftotext`` / ``pandoc`` / ``whisper``) *if it is on PATH*, else a clear error.

The fetched text is written to ``codegraph-out/sources/<slug>.md`` (regenerable,
outside the walked tree) and indexed with the same heading -> ``section`` node
extractor the document tier uses, so ``query`` reaches it immediately.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import ids
from .config import db_path, out_dir
from .db import Db

_UA = {"User-Agent": "codegraph/0.3 (+https://example.invalid)"}
_ARXIV = re.compile(r"arxiv\.org/(abs|pdf)/(?P<id>[\w.\-/]+?)(?:v\d+)?(?:\.pdf)?/?$", re.I)
_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.I | re.S)
_WS = re.compile(r"[ \t]+")
_BLANKS = re.compile(r"\n{3,}")


class IngestError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# fetchers
# --------------------------------------------------------------------------- #

def _get(url: str, *, accept: str = "*/*") -> bytes:
    req = urllib.request.Request(url, headers={**_UA, "Accept": accept})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise IngestError(f"fetch failed: {e}") from e


def _html_to_text(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    text = _SCRIPT.sub(" ", text)
    title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    if m:
        title = html.unescape(_TAG.sub("", m.group(1))).strip()
    body = html.unescape(_TAG.sub("\n", text))
    body = _WS.sub(" ", body)
    body = _BLANKS.sub("\n\n", body).strip()
    return (f"# {title}\n\n" if title else "") + body


def _arxiv(arxiv_id: str) -> tuple[str, str]:
    xml = _get(f"http://export.arxiv.org/api/query?id_list={arxiv_id}",
               accept="application/atom+xml").decode("utf-8", "replace")

    def _tag(name: str) -> str:
        m = re.search(rf"<{name}>(.*?)</{name}>", xml, re.S)
        return _WS.sub(" ", html.unescape(m.group(1)).strip()) if m else ""

    title = _tag("title") or arxiv_id
    summary = _tag("summary")
    authors = ", ".join(re.findall(r"<name>(.*?)</name>", xml))
    md = f"# {title}\n\n"
    if authors:
        md += f"**Authors:** {authors}\n\n"
    md += f"## Abstract\n\n{summary}\n"
    return title, md


def _get_json(url: str, headers: dict, *, method: str = "GET",
              body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={**_UA, "Accept": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise IngestError(f"{e.code}: {e.read()[:200].decode('utf-8', 'replace')}") from e
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        raise IngestError(str(e)) from e


def _notion(page_id: str) -> tuple[str, str]:
    import os

    token = os.environ.get("NOTION_TOKEN") or os.environ.get("NOTION_API_KEY")
    if not token:
        raise IngestError("Notion ingestion needs $NOTION_TOKEN")
    page_id = page_id.replace("-", "")
    h = {"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"}

    page = _get_json(f"https://api.notion.com/v1/pages/{page_id}", h)
    title = "Notion page"
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title" and prop.get("title"):
            title = "".join(t.get("plain_text", "") for t in prop["title"]) or title
            break

    def _rich(block: dict) -> str:
        t = block.get("type", "")
        rt = block.get(t, {}).get("rich_text", []) if isinstance(block.get(t), dict) else []
        return "".join(r.get("plain_text", "") for r in rt)

    lines: list[str] = [f"# {title}", ""]

    def _walk(bid: str, depth: int) -> None:
        cursor = None
        while True:
            url = f"https://api.notion.com/v1/blocks/{bid}/children?page_size=100"
            if cursor:
                url += f"&start_cursor={cursor}"
            resp = _get_json(url, h)
            for b in resp.get("results", []):
                bt = b.get("type", "")
                text = _rich(b)
                if bt == "heading_1":
                    lines.append(f"\n## {text}")
                elif bt == "heading_2":
                    lines.append(f"\n### {text}")
                elif bt == "heading_3":
                    lines.append(f"\n#### {text}")
                elif bt in ("bulleted_list_item", "numbered_list_item"):
                    lines.append(f"{'  ' * depth}- {text}")
                elif bt == "code":
                    lines.append(f"```\n{text}\n```")
                elif bt == "quote":
                    lines.append(f"> {text}")
                elif text:
                    lines.append(text)
                if b.get("has_children") and depth < 4:
                    _walk(b["id"], depth + 1)
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")

    _walk(page_id, 0)
    return title, "\n".join(lines) + "\n"


def _confluence(ref: str) -> tuple[str, str]:
    import os
    import re as _re

    base = os.environ.get("CONFLUENCE_BASE_URL", "").rstrip("/")
    token = os.environ.get("CONFLUENCE_TOKEN") or os.environ.get("CONFLUENCE_API_TOKEN")
    user = os.environ.get("CONFLUENCE_USER", "")
    if ref.startswith("http"):
        m = _re.search(r"/pages/(\d+)", ref) or _re.search(r"pageId=(\d+)", ref)
        if not base:
            base = _re.match(r"(https?://[^/]+(?:/wiki)?)", ref).group(1)
        page_id = m.group(1) if m else ref.rstrip("/").split("/")[-1]
    else:
        page_id = ref
    if not base or not token:
        raise IngestError("Confluence ingestion needs $CONFLUENCE_BASE_URL and "
                          "$CONFLUENCE_TOKEN")
    auth = f"Basic {__import__('base64').b64encode(f'{user}:{token}'.encode()).decode()}" \
        if user else f"Bearer {token}"
    resp = _get_json(
        f"{base}/rest/api/content/{page_id}?expand=body.storage", {"Authorization": auth})
    title = resp.get("title") or "Confluence page"
    storage = ((resp.get("body") or {}).get("storage") or {}).get("value", "")
    return title, f"# {title}\n\n" + _html_to_text(storage.encode())


def _via_cli(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".md", ".markdown", ".txt", ".rst", ".text"):
        return path.read_text(encoding="utf-8", errors="replace")
    if ext == ".pdf":
        if shutil.which("pdftotext"):
            out = subprocess.run(["pdftotext", "-layout", str(path), "-"],
                                 capture_output=True, text=True, timeout=120)
            if out.returncode == 0:
                return out.stdout
        raise IngestError("PDF ingestion needs `pdftotext` (poppler) on PATH")
    if ext in (".docx", ".pptx", ".odt", ".rtf", ".epub", ".html", ".htm"):
        if shutil.which("pandoc"):
            out = subprocess.run(["pandoc", "-t", "markdown", str(path)],
                                 capture_output=True, text=True, timeout=120)
            if out.returncode == 0:
                return out.stdout
        raise IngestError(f"{ext} ingestion needs `pandoc` on PATH")
    if ext in (".mp3", ".mp4", ".wav", ".m4a", ".webm", ".mov", ".mkv", ".flac"):
        exe = shutil.which("whisper") or shutil.which("whisper-cli")
        if exe:
            with __import__("tempfile").TemporaryDirectory() as td:
                r = subprocess.run([exe, str(path), "--model", "base",
                                    "--output_format", "txt", "--output_dir", td],
                                   capture_output=True, text=True, timeout=1800)
                txts = list(Path(td).glob("*.txt"))
                if r.returncode == 0 and txts:
                    return txts[0].read_text(encoding="utf-8", errors="replace")
        raise IngestError("audio/video ingestion needs `whisper` on PATH")
    raise IngestError(f"don't know how to ingest {ext or path.name}")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def _resolve_text(source: str) -> tuple[str, str]:
    """Return ``(suggested_title, markdown_text)``."""
    if source.startswith("notion:"):
        return _notion(source[len("notion:"):])
    if source.startswith("confluence:"):
        return _confluence(source[len("confluence:"):])
    if source.startswith(("http://", "https://")):
        if "notion.so" in source or "notion.site" in source:
            return _notion(source.rstrip("/").split("-")[-1].split("/")[-1])
        if "/wiki/" in source and "atlassian.net" in source:
            return _confluence(source)
        m = _ARXIV.search(source)
        if m:
            return _arxiv(m.group("id"))
        raw = _get(source, accept="text/html,application/xhtml+xml")
        text = _html_to_text(raw)
        title = text.splitlines()[0].lstrip("# ").strip() if text else source
        return title, text
    p = Path(source).expanduser()
    if not p.is_file():
        raise IngestError(f"no such file: {source}")
    return p.stem, _via_cli(p)


def add(root: Path | str, source: str, *, title: str | None = None,
        rebuild: bool = True) -> dict:
    root = Path(root).resolve()
    if not db_path(root).exists():
        raise IngestError(f"no graph at {root} — run `codegraph extract` first")

    got_title, text = _resolve_text(source)
    title = title or got_title
    slug_seed = re.sub(r"[^\w.-]+", "-", title.lower()).strip("-")[:60] or "source"
    sources_dir = out_dir(root) / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    md_path = sources_dir / f"{slug_seed}.md"

    frontmatter = (f"<!-- codegraph source: {source}\n"
                   f"     fetched: {time.strftime('%Y-%m-%d %H:%M:%S')} -->\n\n")
    md_path.write_text(frontmatter + text, encoding="utf-8")

    rel = f"{out_dir(root).name}/sources/{slug_seed}.md"
    from .extract.docs import extract_doc

    res = extract_doc(rel, text, ".md")
    for n in res.nodes:
        n["file_type"] = "document"

    db = Db(db_path(root), create=False)
    import hashlib

    sha = hashlib.sha256(text.encode()).hexdigest()
    db.replace_file(rel, res.nodes, res.edges, raw_calls=(), origin="ast")
    db.upsert_file(path=rel, abs_identity=md_path.resolve().as_posix(),
                   file_type="document", lang="markdown", content_sha256=sha,
                   ast_hash=sha, semantic_hash="", mtime=md_path.stat().st_mtime,
                   seen=time.time(), status="present")
    db.reindex_fts()
    db.recompute_degrees()

    sections = sum(1 for n in res.nodes if n["kind"] == "section")
    if rebuild:
        from .analyze import analyze
        from .cluster import cluster
        from .render.graph_json import write_graph_json
        from .render.report import write_report

        cluster(db)
        analyze(db)
        write_graph_json(db, out_dir(root))
        write_report(db, out_dir(root))
    db.close()
    return {"title": title, "path": str(md_path), "rel": rel,
            "sections": sections, "chars": len(text)}
