from codegraph.config import db_path
from codegraph.db import Db
from codegraph.pipeline import extract
from codegraph.context import context


def _mkproj(root):
    (root / "auth.py").write_text(
        "def login(user):\n"
        "    return check_password(user)\n\n"
        "def check_password(user):\n"
        "    return user is not None\n"
    )
    (root / "api.py").write_text(
        "from auth import login\n\n"
        "def handle_request(req):\n"
        "    return login(req)\n"
    )


def test_context_bundles_body_and_signatures(tmp_path):
    _mkproj(tmp_path)
    extract(tmp_path, semantic="none")
    db = Db(db_path(tmp_path), create=False)
    out = context(db, "login")
    db.close()
    # target body present with line numbers
    assert "## definition" in out
    assert "return check_password(user)" in out
    # caller signature (handle_request) present, not its body
    assert "callers" in out
    assert "handle_request" in out
    # callee (check_password) listed
    assert "callees" in out
    assert "check_password" in out
    # anchors
    assert "api.py:" in out and "auth.py:" in out


def test_context_unknown_symbol(tmp_path):
    _mkproj(tmp_path)
    extract(tmp_path, semantic="none")
    db = Db(db_path(tmp_path), create=False)
    out = context(db, "nonexistent_xyz")
    db.close()
    assert "No node matching" in out
