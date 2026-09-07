from codegraph import ids


def test_normalize_idempotent():
    for s in ["FooBar", "src/auth/session.py", "Ünïcöde", "a__b--c  d", "İstanbul"]:
        once = ids.normalize(s)
        assert ids.normalize(once) == once


def test_normalize_caseless_stable():
    for s in ["FooBar", "HTTPServer", "İstanbul", "ΣΊΣΥΦΟΣ"]:
        assert ids.normalize(s) == ids.normalize(s.casefold())


def test_normalize_charset():
    out = ids.normalize("a.b/c-d e")
    assert set(out) <= set("abcdefghijklmnopqrstuvwxyz0123456789_")
    assert "__" not in out
    assert not out.startswith("_") and not out.endswith("_")


def test_file_stem_keeps_all_segments():
    assert ids.file_stem("docs/v1/api/README.md") == "docs/v1/api/README"
    assert ids.file_stem("setup.py") == "setup"


def test_file_slug():
    assert ids.file_slug("src/auth/session.py") == "src_auth_session"


def test_symbol_slug():
    assert ids.symbol_slug("src/auth/session.py", "Session", "login") == \
        "src_auth_session_session_login"
