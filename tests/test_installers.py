import json

import pytest

from codegraph.installers import PLATFORMS, install


@pytest.mark.parametrize("platform", ["claude", "cursor", "vscode"])
def test_project_scoped_install_writes_config(tmp_path, platform):
    r = install(platform, tmp_path)
    assert r["scope"] == "project"
    cfg = json.loads(open(r["path"]).read())
    key = PLATFORMS[platform].key
    assert "codegraph" in cfg[key]
    assert cfg[key]["codegraph"]["args"][:3] == ["-m", "codegraph", "serve"] or \
        cfg[key]["codegraph"]["command"]["args"][:3] == ["-m", "codegraph", "serve"]


def test_install_merges_existing_servers(tmp_path):
    cfg = tmp_path / ".mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    install("claude", tmp_path)
    data = json.loads(cfg.read_text())
    assert set(data["mcpServers"]) == {"other", "codegraph"}


def test_install_is_idempotent(tmp_path):
    install("cursor", tmp_path)
    install("cursor", tmp_path)
    data = json.loads((tmp_path / ".cursor" / "mcp.json").read_text())
    assert list(data["mcpServers"]) == ["codegraph"]


def test_unknown_platform_rejected(tmp_path):
    with pytest.raises(ValueError, match="unknown platform"):
        install("emacs", tmp_path)


def test_zed_uses_context_servers_shape(tmp_path):
    r = install("zed", tmp_path)
    data = json.loads(open(r["path"]).read())
    assert "codegraph" in data["context_servers"]
    assert data["context_servers"]["codegraph"]["command"]["path"]
