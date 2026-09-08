"""Media tier — images become nodes the vision subagent describes; audio/video
are transcribed when a Whisper backend exists, else stubbed with a hint."""

import json

from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.detect import detect
from codegraph.media import _segments_to_markdown, transcribe
from codegraph.pipeline import extract
from codegraph.semantic import CHUNK_DIR


def test_detect_classifies_media(tmp_path):
    (tmp_path / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 40)
    (tmp_path / "talk.mp3").write_bytes(b"ID3" + b"0" * 40)
    (tmp_path / "demo.mp4").write_bytes(b"0" * 40)
    kinds = {f.rel: f.file_type for f in detect(tmp_path).files}
    assert kinds == {"diagram.png": "image", "talk.mp3": "audio",
                     "demo.mp4": "video"}


def test_image_becomes_a_node_and_lands_in_ideas_request(tmp_path):
    (tmp_path / "app.py").write_text("def run():\n    return 1\n")
    (tmp_path / "ARCHITECTURE.md").write_text("# Arch\n## Flow\ndetail\n")
    (tmp_path / "ui.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    for i in range(6):
        (tmp_path / f"m{i}.py").write_text(f"def g{i}():\n    return {i}\n")

    extract(tmp_path, semantic="skill", scip="none", semantic_chunk_files=3)

    db = Db(db_path(tmp_path), create=False)
    row = db.conn.execute(
        "SELECT kind, file_type FROM nodes WHERE label='ui.png'").fetchone()
    db.close()
    assert row["kind"] == "file" and row["file_type"] == "image"

    ideas = json.loads((out_dir(tmp_path) / CHUNK_DIR / "ideas.json").read_text())
    assert ideas["images"] == ["ui.png"]
    idx = json.loads((out_dir(tmp_path) / "semantic-request.json").read_text())
    assert idx["images"] == 1


def test_audio_without_whisper_is_stubbed_not_fatal(tmp_path, monkeypatch):
    import codegraph.media as media
    monkeypatch.setattr(media, "transcribe", lambda *a, **k: None)

    (tmp_path / "app.py").write_text("def run():\n    return 1\n")
    (tmp_path / "meeting.mp3").write_bytes(b"ID3" + b"0" * 64)
    st = extract(tmp_path, semantic="none", scip="none")
    assert st["media"]["av"] == 1 and st["media"]["transcribed"] == []

    db = Db(db_path(tmp_path), create=False)
    r = db.conn.execute(
        "SELECT kind, file_type, rationale FROM nodes WHERE label='meeting.mp3'"
    ).fetchone()
    db.close()
    assert r["kind"] == "file" and r["file_type"] == "audio"
    assert "Whisper" in (r["rationale"] or "")


def test_transcribe_returns_none_when_no_backend(tmp_path):
    f = tmp_path / "x.wav"
    f.write_bytes(b"RIFF" + b"0" * 40)
    assert transcribe(f) is None  # no faster-whisper / whisper / CLI in CI


def test_segments_to_markdown_blocks_by_time():
    md = _segments_to_markdown("talk.mp3", [
        (0.0, "hello there"), (30.0, "still intro"),
        (200.0, "new topic now"),
    ])
    assert md.startswith("# Transcript: talk.mp3")
    assert "## [0:00]" in md and "## [3:20]" in md
    assert "hello there still intro" in md
