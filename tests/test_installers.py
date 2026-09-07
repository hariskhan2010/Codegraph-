"""Multi-agent installer — MCP config + instructions per agent, both scopes.

Every test runs against a fake ``$HOME`` so nothing touches the real machine.
"""

import json

import pytest

from codegraph import installers as I


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(I, "_home", lambda: home)
    monkeypatch.setenv("APPDATA", str(home / "AppData"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(I.shutil, "which", lambda name: "codegraph")  # stable command
    return home


@pytest.fixture
def project(tmp_path):
    p = tmp_path / "proj"
    p.mkdir()
    (p / "m.py").write_text("def f():\n    return 1\n")
    return p


def _read_json(path):
    return json.loads(open(path).read())


# --------------------------------------------------------------------------- #
# MCP config shapes
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("key", ["claude", "cursor", "gemini", "qwen"])
def test_json_map_agents_write_mcpServers(fake_home, project, key):
    recs = I.install(key, project, scope="user")
    mcp = next(r for r in recs if r["what"] == "mcp")
    data = _read_json(mcp["path"])
    assert data["mcpServers"]["codegraph"]["command"] == "codegraph"
    assert data["mcpServers"]["codegraph"]["args"] == ["serve"]      # user scope: no path


def test_project_scope_pins_the_path(fake_home, project):
    recs = I.install("gemini", project, scope="project")
    data = _read_json(next(r for r in recs if r["what"] == "mcp")["path"])
    assert data["mcpServers"]["codegraph"]["args"] == ["serve", str(project.resolve())]


def test_vscode_uses_servers_key(fake_home, project):
    recs = I.install("vscode", project, scope="project")
    data = _read_json(next(r for r in recs if r["what"] == "mcp")["path"])
    assert "codegraph" in data["servers"] and "mcpServers" not in data


def test_zed_context_servers_shape(fake_home, project):
    recs = I.install("zed", project, scope="user")
    data = _read_json(next(r for r in recs if r["what"] == "mcp")["path"])
    assert data["context_servers"]["codegraph"]["command"]["path"] == "codegraph"


def test_opencode_local_shape(fake_home, project):
    recs = I.install("opencode", project, scope="project")
    data = _read_json(next(r for r in recs if r["what"] == "mcp")["path"])
    entry = data["mcp"]["codegraph"]
    assert entry["type"] == "local" and entry["enabled"] is True
    assert entry["command"][0] == "codegraph"


def test_codex_toml_block(fake_home, project):
    path = fake_home / ".codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text('model = "gpt-5"\n\n[other]\nx = 1\n')
    I.install("codex", project, scope="user")
    text = path.read_text()
    assert 'model = "gpt-5"' in text and "[other]" in text          # preserved
    assert "[mcp_servers.codegraph]" in text
    assert 'command = "codegraph"' in text and 'args = ["serve"]' in text


def test_continue_standalone_yaml(fake_home, project):
    recs = I.install("continue", project, scope="project")
    text = open(next(r for r in recs if r["what"] == "mcp")["path"]).read()
    assert "name: codegraph" in text and '- "serve"' in text


def test_single_scope_agent_falls_back_with_note(fake_home, project):
    # vscode has no user MCP file -> a global install lands in the project, noted
    recs = I.install("vscode", project, scope="user")
    mcp = next(r for r in recs if r["what"] == "mcp")
    assert "project" in mcp.get("note", "")


# --------------------------------------------------------------------------- #
# instructions
# --------------------------------------------------------------------------- #

def test_claude_gets_skill_md(fake_home, project):
    recs = I.install("claude", project, scope="user")
    skill = next(r for r in recs if r["what"] == "skill-md")
    assert skill["path"].endswith("SKILL.md")
    assert "codegraph" in open(skill["path"]).read().lower()


def test_gemini_gets_slash_command_toml(fake_home, project):
    recs = I.install("gemini", project, scope="user")
    toml = next(r for r in recs if r["what"] == "commands-toml")
    text = open(toml["path"]).read()
    assert text.startswith("description =") and 'prompt = """' in text


def test_agents_md_snippet_is_idempotent(fake_home, project):
    I.install("codex", project, scope="project")
    a = (project / "AGENTS.md").read_text()
    I.install("codex", project, scope="project")
    b = (project / "AGENTS.md").read_text()
    assert a == b
    assert a.count("<!-- codegraph:start -->") == 1


def test_agents_md_preserves_surrounding_content(fake_home, project):
    (project / "AGENTS.md").write_text("# My project rules\n\nBe nice.\n")
    I.install("codex", project, scope="project")
    text = (project / "AGENTS.md").read_text()
    assert "Be nice." in text and "<!-- codegraph:start -->" in text


def test_user_scope_skips_project_only_instructions(fake_home, project):
    # opencode's AGENTS.md is project-only; a global install must not drop it in cwd
    recs = I.install("opencode", project, scope="user")
    assert not any(r["what"] == "agents-md" for r in recs)


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #

def test_unknown_agent_rejected(fake_home, project):
    with pytest.raises(ValueError, match="unknown agent"):
        I.install("emacs", project)


def test_manual_agents_only_print(fake_home, project):
    recs = I.install("kimi", project)
    assert recs == [{"what": "manual", "path": I.AGENTS["kimi"].manual}]


def test_detected_reflects_existing_dir(fake_home):
    assert not I.detected("gemini")
    (fake_home / ".gemini").mkdir()
    assert I.detected("gemini")


def test_reinstall_no_duplicate_mcp_entry(fake_home, project):
    I.install("cursor", project, scope="user")
    I.install("cursor", project, scope="user")
    data = _read_json(fake_home / ".cursor" / "mcp.json")
    assert list(data["mcpServers"]) == ["codegraph"]


def test_menu_has_eleven_agents():
    assert len(I.MENU) == 11
    assert all(a.manual is None for a in I.MENU)
