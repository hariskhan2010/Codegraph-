from codegraph.detect import detect, is_sensitive


def test_sensitive_patterns():
    assert is_sensitive(".env")
    assert is_sensitive("config/id_rsa")
    assert is_sensitive("deploy/server.pem")
    assert is_sensitive(".aws/credentials")
    assert is_sensitive("secrets/db.txt")
    assert is_sensitive("my_password.txt")
    assert not is_sensitive(".env.example")
    assert not is_sensitive("src/auth/session.py")
    assert not is_sensitive("secrets/loader.py")  # graphable source under secrets/


def test_detect_classifies_and_skips(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x = 1\n")
    (tmp_path / "README.md").write_text("# hi\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("//\n")
    (tmp_path / ".gitignore").write_text("*.log\n")
    (tmp_path / "debug.log").write_text("noise\n")

    det = detect(tmp_path)
    rels = {f.rel for f in det.files}
    assert "src/app.py" in rels
    assert "README.md" in rels
    assert not any("node_modules" in r for r in rels)
    assert not any(r == ".env" for r in rels)
    assert not any(r == "debug.log" for r in rels)
    assert any(".env" in s for s in det.skipped_sensitive)
    assert [f.file_type for f in det.files if f.rel == "src/app.py"] == ["code"]
