"""Audio / video transcription — the one media capability that needs a real
model, not a subagent.

Images are handled in the semantic pass (a vision subagent reads them). Audio and
video can't be: they are transcribed here to Markdown and then flow through the
normal document tier. Transcription is best-effort across three backends, in
order of quality:

1. ``faster-whisper``   (``pip install code-graph[media]``)
2. ``openai-whisper``   (``pip install openai-whisper``)
3. a ``whisper`` CLI on ``PATH``

If none is available the caller gets ``None`` and the file becomes a stub node
with a one-line hint — never a hard failure (graphify aborts the whole run when
Whisper is missing; #1392).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

_AV_SUFFIXES = {
    ".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".aac", ".opus", ".wma",
}


def is_av(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _AV_SUFFIXES


def _fmt_ts(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:d}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 \
        else f"{s // 60:d}:{s % 60:02d}"


def _segments_to_markdown(name: str, segments: list[tuple[float, str]]) -> str:
    """``[(start_seconds, text), …]`` -> a Markdown transcript, one ``##`` per
    ~2-minute block so the document tier makes navigable section nodes."""
    lines = [f"# Transcript: {name}", ""]
    block_start = None
    buf: list[str] = []

    def flush() -> None:
        if buf:
            lines.append(f"## [{_fmt_ts(block_start)}]")
            lines.append(" ".join(buf).strip())
            lines.append("")

    for start, text in segments:
        text = text.strip()
        if not text:
            continue
        if block_start is None or start - block_start >= 120:
            flush()
            block_start, buf = start, []
        buf.append(text)
    flush()
    return "\n".join(lines).rstrip() + "\n"


def _try_faster_whisper(path: Path, model: str) -> list[tuple[float, str]] | None:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return None
    m = WhisperModel(model, device="auto", compute_type="int8")
    segs, _ = m.transcribe(str(path))
    return [(s.start, s.text) for s in segs]


def _try_openai_whisper(path: Path, model: str) -> list[tuple[float, str]] | None:
    try:
        import whisper  # type: ignore
    except ImportError:
        return None
    m = whisper.load_model(model)
    res = m.transcribe(str(path))
    return [(s["start"], s["text"]) for s in res.get("segments", [])]


def _try_whisper_cli(path: Path, model: str, out: Path) -> list[tuple[float, str]] | None:
    exe = shutil.which("whisper")
    if not exe:
        return None
    out.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [exe, str(path), "--model", model, "--output_format", "json",
         "--output_dir", str(out)],
        capture_output=True, text=True, timeout=3600,
    )
    if r.returncode != 0:
        return None
    js = out / (path.stem + ".json")
    if not js.exists():
        return None
    data = json.loads(js.read_text(encoding="utf-8"))
    return [(s.get("start", 0.0), s.get("text", "")) for s in data.get("segments", [])]


def transcribe(path: Path, *, model: str = "base",
               scratch: Path | None = None) -> tuple[str, str] | None:
    """``(markdown_transcript, backend_name)`` or ``None`` if no backend works."""
    path = Path(path)
    scratch = scratch or (path.parent / ".codegraph-transcribe")
    for name, fn in (
        ("faster-whisper", lambda: _try_faster_whisper(path, model)),
        ("openai-whisper", lambda: _try_openai_whisper(path, model)),
        ("whisper-cli", lambda: _try_whisper_cli(path, model, scratch)),
    ):
        try:
            segs = fn()
        except Exception:
            continue
        if segs:
            return _segments_to_markdown(path.name, segs), name
    return None
