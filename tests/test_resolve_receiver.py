"""Receiver-aware cross-file call resolution (the accuracy fix).

`x.execute()` must not collapse onto the one global `execute()` definition just
because the name is unique. A method call resolves only when the receiver's
class is known (`self`, a constructor, a local `x = Foo()`, or `Foo.bar()`), or
the name is rare enough to be unambiguous. Common method names called from many
files with an unknown receiver stay unresolved — no more manufactured god nodes.
"""

from codegraph.config import db_path
from codegraph.db import Db
from codegraph.pipeline import extract


def _calls(project):
    db = Db(db_path(project), create=False)
    by_id = {int(r["id"]): r for r in db.nodes()}
    pairs = {
        (by_id[int(e["src"])]["label"], by_id[int(e["dst"])]["label"],
         e["confidence"], e["evidence"])
        for e in db.edges() if e["relation"] == "calls"
    }
    db.close()
    return pairs


def test_unknown_receiver_common_name_is_not_linked(tmp_path):
    (tmp_path / "engine.py").write_text(
        "class Engine:\n"
        "    def execute(self, q):\n"
        "        return q\n"
    )
    # five files that each call `.execute()` on an unknown receiver
    for i in range(5):
        (tmp_path / f"user{i}.py").write_text(
            f"def work{i}(conn):\n"
            f"    return conn.execute('select 1')\n"
        )
    extract(tmp_path, semantic="none")
    linked = {(s, d) for s, d, *_ in _calls(tmp_path)}
    assert not any(d == ".execute()" for _, d in linked), linked


def test_self_call_resolves_to_own_class(tmp_path):
    (tmp_path / "a.py").write_text(
        "class Worker:\n"
        "    def run(self):\n"
        "        return self.step()\n"
        "    def step(self):\n"
        "        return 1\n"
    )
    extract(tmp_path, semantic="none")
    assert (".run()", ".step()", "EXTRACTED", "same-file") in _calls(tmp_path)


def test_constructor_and_local_binding_resolve_cross_file(tmp_path):
    (tmp_path / "svc.py").write_text(
        "class Mailer:\n"
        "    def deliver(self, msg):\n"
        "        return msg\n"
    )
    (tmp_path / "app.py").write_text(
        "from svc import Mailer\n"
        "def notify(msg):\n"
        "    m = Mailer()\n"
        "    return m.deliver(msg)\n"
    )
    extract(tmp_path, semantic="none")
    hit = [(c, ev) for s, d, c, ev in _calls(tmp_path)
           if s == "notify()" and d == ".deliver()"]
    assert hit and hit[0][1] == "xfile-recv", _calls(tmp_path)


def test_rare_bare_call_still_resolves_without_import(tmp_path):
    (tmp_path / "math_helpers.py").write_text(
        "def compute_trajectory(v):\n    return v * 2\n"
    )
    (tmp_path / "sim.py").write_text(
        "def frame(v):\n    return compute_trajectory(v)\n"
    )
    extract(tmp_path, semantic="none")
    assert any(d == "compute_trajectory()" and s == "frame()"
               for s, d, *_ in _calls(tmp_path)), _calls(tmp_path)


def test_untyped_receiver_rare_name_links_only_as_weak_inference(tmp_path):
    (tmp_path / "shapes.py").write_text(
        "class Circle:\n"
        "    def area(self):\n"
        "        return 3\n"
    )
    (tmp_path / "use.py").write_text(
        "def go(sq):\n"
        "    return sq.area()\n"  # sq untyped: a guess, must be flagged as one
    )
    extract(tmp_path, semantic="none")
    hits = [(c, sc) for s, d, c, sc in
            ((s, d, c, ev) for s, d, c, ev in _calls_scored(tmp_path))
            if s == "go()" and d == ".area()"]
    for conf, score in hits:
        assert conf == "INFERRED" and score <= 0.65, hits


def test_untyped_receiver_popular_name_is_dropped(tmp_path):
    (tmp_path / "store.py").write_text(
        "class Store:\n"
        "    def save(self, x):\n"
        "        return x\n"
    )
    (tmp_path / "use.py").write_text(
        "def go(thing):\n"
        "    return thing.save()\n"  # `save` is an always-popular name
    )
    extract(tmp_path, semantic="none")
    assert not any(d == ".save()" for s, d, *_ in _calls(tmp_path)
                   if s == "go()"), _calls(tmp_path)


def _calls_scored(project):
    db = Db(db_path(project), create=False)
    by_id = {int(r["id"]): r for r in db.nodes()}
    rows = {
        (by_id[int(e["src"])]["label"], by_id[int(e["dst"])]["label"],
         e["confidence"], e["confidence_score"])
        for e in db.edges() if e["relation"] == "calls"
    }
    db.close()
    return rows
