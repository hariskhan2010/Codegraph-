"""Framework route handlers get path-qualified labels so `POST` in every
Next.js `route.ts` is distinguishable; docs don't crowd the report."""

from codegraph.config import db_path, out_dir
from codegraph.db import Db
from codegraph.extract.engine import _route_prefix
from codegraph.pipeline import extract


def test_route_prefix_shapes():
    assert _route_prefix("src/app/api/cart/route.ts") == "api/cart"
    assert _route_prefix("app/(shop)/products/[id]/route.ts") == "products/[id]"
    assert _route_prefix("src/app/route.ts") == "/"
    assert _route_prefix("pages/api/checkout.ts") == "api/checkout"
    assert _route_prefix("pages/api/index.ts") == "api"
    assert _route_prefix("pages/index.tsx") == "/"
    assert _route_prefix("src/lib/util.ts") is None


def test_nextjs_handlers_are_disambiguated(tmp_path):
    for path in ("src/app/api/payments/create-session/route.ts",
                 "src/app/api/auth/login/route.ts"):
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            "export async function POST(req) { return helper(); }\n"
            "function helper() { return 1; }\n"
        )
    extract(tmp_path, semantic="none", scip="none")
    db = Db(db_path(tmp_path), create=False)
    labels = {r["label"] for r in db.nodes()}
    kinds = {r["kind"] for r in db.nodes()}
    db.close()
    assert "POST api/payments/create-session" in labels
    assert "POST api/auth/login" in labels
    assert "POST()" not in labels
    assert "route" in kinds


def test_report_splits_code_and_docs(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "code.ts").write_text(
        "export function core() { return leaf(); }\nfunction leaf() { return 1; }\n"
    )
    (tmp_path / "docs").mkdir()
    for i in range(3):
        (tmp_path / "docs" / f"note{i}.md").write_text(
            f"# Doc {i}\n## Section A{i}\ntext\n## Section B{i}\nmore\n"
        )
    extract(tmp_path, semantic="none", scip="none")
    md = (out_dir(tmp_path) / "GRAPH_REPORT.md").read_text()
    assert "### Code" in md and "### Documentation" in md
    # a doc heading must not appear in the god-nodes list
    gods_block = md.split("## God Nodes")[1].split("##")[0]
    assert "Section A" not in gods_block
