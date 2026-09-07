import json

from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.merge import merge
from codegraph.pipeline import check_update, extract, update


def _repo(root, name):
    (root / f"{name}.py").write_text(
        f"def {name}_entry():\n    return {name}_helper()\n\n"
        f"def {name}_helper():\n    return 1\n"
    )


def test_check_update_detects_change(tmp_path):
    _repo(tmp_path, "svc")
    extract(tmp_path, semantic="none")
    assert check_update(tmp_path)["stale"] is False

    (tmp_path / "svc.py").write_text("def svc_entry():\n    return 2\n")
    r = check_update(tmp_path)
    assert r["stale"] is True
    assert "svc.py" in r["changed"]

    (tmp_path / "new_mod.py").write_text("def f():\n    return 0\n")
    r = check_update(tmp_path)
    assert "new_mod.py" in r["added"]

    update(tmp_path)
    assert check_update(tmp_path)["stale"] is False


def test_merge_graphs_keeps_sources_distinct(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _repo(a, "alpha")
    _repo(b, "beta")
    extract(a, semantic="none")
    extract(b, semantic="none")

    out = tmp_path / "merged"
    r = merge([a, b], out, ["alpha", "beta"])
    assert r["nodes"] == 6  # 2 files + 4 functions

    gj = json.loads((out_dir(out) / "graph.json").read_text())
    files = {n["source_file"] for n in gj["nodes"]}
    assert any(f.startswith("alpha/") for f in files)
    assert any(f.startswith("beta/") for f in files)

    db = Db(db_path(out), create=False)
    by_id = {int(n["id"]): n for n in db.nodes()}
    calls = {
        (by_id[int(e["src"])]["label"], by_id[int(e["dst"])]["label"])
        for e in db.edges() if e["relation"] == "calls"
    }
    db.close()
    assert ("alpha_entry()", "alpha_helper()") in calls
    assert ("beta_entry()", "beta_helper()") in calls
