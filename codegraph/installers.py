"""``codegraph install`` — wire codegraph into an AI coding agent.

Every agent speaks MCP but keeps its config in a different file with a different
shape, and each has its own place for "assistant instructions" (a skill, a slash
command, an ``AGENTS.md``). This module knows, per agent:

* where the MCP config lives at **user** and/or **project** scope, and its shape
* how to drop the "use codegraph first" instructions in that agent's own format

All writes are read-merge-write and idempotent. ``install()`` returns a list of
``{"what": ..., "path": ...}`` records describing what it wrote.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Callable

SERVER_NAME = "codegraph"


# --------------------------------------------------------------------------- #
# paths / command
# --------------------------------------------------------------------------- #

def _home() -> Path:
    return Path.home()


def _appdata() -> Path:
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming"))
    return Path(os.environ.get("XDG_CONFIG_HOME", _home() / ".config"))


def server_command() -> tuple[str, list[str]]:
    """``(command, base_args)`` the MCP entry should run — the bare ``codegraph``
    console script when it is on PATH (portable across venv moves), else
    ``<python> -m codegraph``."""
    if shutil.which(SERVER_NAME):
        return SERVER_NAME, []
    return sys.executable, ["-m", SERVER_NAME]


def _serve(scope: str, root: Path) -> tuple[str, list[str]]:
    cmd, base = server_command()
    args = [*base, "serve"]
    if scope == "project":
        args.append(str(root.resolve()))
    return cmd, args


# --------------------------------------------------------------------------- #
# agent registry
# --------------------------------------------------------------------------- #

@dataclass
class Instr:
    kind: str                                   # skill-md | commands-toml | agents-md
    user: Callable[[], Path] | None = None
    project: Callable[[Path], Path] | None = None


@dataclass
class Agent:
    key: str
    label: str
    mcp_format: str | None = None               # json-map|json-servers|zed|toml-codex|opencode|yaml-continue
    mcp_user: Callable[[], Path] | None = None
    mcp_project: Callable[[Path], Path] | None = None
    instr: list[Instr] = field(default_factory=list)
    manual: str | None = None                   # print-only agents (kimi, cline)
    note: str = ""
    detect: Callable[[], bool] | None = None

    def in_menu(self) -> bool:
        return self.manual is None


def _zed_settings() -> Path:
    name = "Zed" if sys.platform == "darwin" else "zed"
    return _appdata() / name / "settings.json"


AGENTS: dict[str, Agent] = {
    "claude": Agent(
        "claude", "Claude Code", "json-map",
        mcp_user=lambda: _home() / ".claude.json",
        mcp_project=lambda r: r / ".mcp.json",
        instr=[Instr("skill-md",
                     lambda: _home() / ".claude" / "skills" / "codegraph" / "SKILL.md",
                     lambda r: r / ".claude" / "skills" / "codegraph" / "SKILL.md")],
        detect=lambda: (_home() / ".claude").exists() or (_home() / ".claude.json").exists(),
    ),
    "claude-desktop": Agent(
        "claude-desktop", "Claude Desktop", "json-map",
        mcp_user=lambda: _appdata() / "Claude" / "claude_desktop_config.json",
        detect=lambda: (_appdata() / "Claude").exists(),
    ),
    "cursor": Agent(
        "cursor", "Cursor", "json-map",
        mcp_user=lambda: _home() / ".cursor" / "mcp.json",
        mcp_project=lambda r: r / ".cursor" / "mcp.json",
        instr=[Instr("agents-md",
                     lambda: _home() / ".cursor" / "AGENTS.md",
                     lambda r: r / "AGENTS.md")],
        detect=lambda: (_home() / ".cursor").exists(),
    ),
    "windsurf": Agent(
        "windsurf", "Windsurf", "json-map",
        mcp_user=lambda: _home() / ".codeium" / "windsurf" / "mcp_config.json",
        detect=lambda: (_home() / ".codeium" / "windsurf").exists(),
    ),
    "vscode": Agent(
        "vscode", "VS Code (Copilot)", "json-servers",
        mcp_project=lambda r: r / ".vscode" / "mcp.json",
        instr=[Instr("agents-md", None, lambda r: r / "AGENTS.md")],
        detect=lambda: (_home() / ".vscode").exists()
        or (_appdata() / "Code").exists(),
    ),
    "zed": Agent(
        "zed", "Zed", "zed",
        mcp_user=_zed_settings,
        instr=[Instr("agents-md", None, lambda r: r / "AGENTS.md")],
        detect=lambda: _zed_settings().parent.exists(),
    ),
    "gemini": Agent(
        "gemini", "Gemini CLI", "json-map",
        mcp_user=lambda: _home() / ".gemini" / "settings.json",
        mcp_project=lambda r: r / ".gemini" / "settings.json",
        instr=[
            Instr("commands-toml",
                  lambda: _home() / ".gemini" / "commands" / "codegraph.toml",
                  lambda r: r / ".gemini" / "commands" / "codegraph.toml"),
            Instr("agents-md",
                  lambda: _home() / ".gemini" / "GEMINI.md",
                  lambda r: r / "GEMINI.md"),
        ],
        detect=lambda: (_home() / ".gemini").exists(),
    ),
    "qwen": Agent(
        "qwen", "Qwen Code", "json-map",
        mcp_user=lambda: _home() / ".qwen" / "settings.json",
        mcp_project=lambda r: r / ".qwen" / "settings.json",
        instr=[
            Instr("commands-toml",
                  lambda: _home() / ".qwen" / "commands" / "codegraph.toml",
                  lambda r: r / ".qwen" / "commands" / "codegraph.toml"),
            Instr("agents-md",
                  lambda: _home() / ".qwen" / "QWEN.md",
                  lambda r: r / "QWEN.md"),
        ],
        detect=lambda: (_home() / ".qwen").exists(),
    ),
    "codex": Agent(
        "codex", "Codex CLI", "toml-codex",
        mcp_user=lambda: _home() / ".codex" / "config.toml",
        mcp_project=lambda r: r / ".codex" / "config.toml",
        instr=[Instr("agents-md",
                     lambda: _home() / ".codex" / "AGENTS.md",
                     lambda r: r / "AGENTS.md")],
        detect=lambda: (_home() / ".codex").exists(),
    ),
    "opencode": Agent(
        "opencode", "OpenCode", "opencode",
        mcp_user=lambda: _appdata() / "opencode" / "opencode.json",
        mcp_project=lambda r: r / "opencode.json",
        instr=[Instr("agents-md", None, lambda r: r / "AGENTS.md")],
        detect=lambda: (_appdata() / "opencode").exists(),
    ),
    "continue": Agent(
        "continue", "Continue", "yaml-continue",
        mcp_user=lambda: _home() / ".continue" / "mcpServers" / "codegraph.yaml",
        mcp_project=lambda r: r / ".continue" / "mcpServers" / "codegraph.yaml",
        instr=[Instr("agents-md",
                     lambda: _home() / ".continue" / "rules" / "codegraph.md",
                     lambda r: r / ".continue" / "rules" / "codegraph.md")],
        detect=lambda: (_home() / ".continue").exists(),
    ),
    # print-only — no stable config path to write
    "kimi": Agent(
        "kimi", "Kimi Code", None,
        manual="Run:  kimi --mcp-config-file <(codegraph mcp-config)\n"
               "  or add codegraph in-session with  /mcp-config",
    ),
    "cline": Agent(
        "cline", "Cline (VS Code)", None,
        manual="In VS Code: Cline panel -> MCP Servers -> Configure, then add:\n"
               '  "codegraph": {"command": "codegraph", "args": ["serve"]}',
    ),
}

MENU = [a for a in AGENTS.values() if a.in_menu()]

_GLM_KIMI_NOTE = (
    "Note: GLM and Kimi as *models* run through one of the agents above (or "
    "Claude Code) by pointing it at their API endpoint - pick that agent."
)


def detected(key: str) -> bool:
    a = AGENTS.get(key)
    try:
        return bool(a and a.detect and a.detect())
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# bundled instruction text
# --------------------------------------------------------------------------- #

def _snippet() -> str:
    return (resources.files("codegraph.skill") / "AGENTS_SNIPPET.md").read_text(
        encoding="utf-8"
    ).strip() + "\n"


_MARK_RE = re.compile(r"<!-- codegraph:start -->.*?<!-- codegraph:end -->",
                      re.DOTALL)


def _snippet_body() -> str:
    """The snippet without the HTML markers — for a slash-command prompt."""
    return re.sub(r"<!-- codegraph:(start|end) -->\n?", "", _snippet()).strip() + "\n"


# --------------------------------------------------------------------------- #
# MCP writers
# --------------------------------------------------------------------------- #

def _load_json(p: Path) -> dict:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")) or {}
        except json.JSONDecodeError:
            return {}
    return {}


def _dump_json(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def _resolve_scoped(user_fn, project_fn, scope: str, root: Path) -> tuple[Path, str] | None:
    """Return ``(path, effective_scope)``. Falls back to the other scope's
    locator when the requested one isn't defined for this agent."""
    if scope == "user" and user_fn is not None:
        return Path(user_fn()), "user"
    if scope == "project" and project_fn is not None:
        return Path(project_fn(root)), "project"
    if user_fn is not None:
        return Path(user_fn()), "user"
    if project_fn is not None:
        return Path(project_fn(root)), "project"
    return None


def _write_mcp(agent: Agent, scope: str, root: Path) -> Path | None:
    res = _resolve_scoped(agent.mcp_user, agent.mcp_project, scope, root)
    if res is None:
        return None
    path, eff_scope = res
    cmd, args = _serve(eff_scope, root)

    fmt = agent.mcp_format
    if fmt in ("json-map", "json-servers"):
        key = "servers" if fmt == "json-servers" else "mcpServers"
        data = _load_json(path)
        data.setdefault(key, {})[SERVER_NAME] = {"command": cmd, "args": args}
        _dump_json(path, data)
    elif fmt == "zed":
        data = _load_json(path)
        data.setdefault("context_servers", {})[SERVER_NAME] = {
            "command": {"path": cmd, "args": args}, "settings": {},
        }
        _dump_json(path, data)
    elif fmt == "opencode":
        data = _load_json(path)
        data.setdefault("$schema", "https://opencode.ai/config.json")
        data.setdefault("mcp", {})[SERVER_NAME] = {
            "type": "local", "command": [cmd, *args], "enabled": True,
        }
        _dump_json(path, data)
    elif fmt == "toml-codex":
        _write_codex_toml(path, cmd, args)
    elif fmt == "yaml-continue":
        _write_continue_yaml(path, cmd, args)
    else:
        return None
    return path


def _write_codex_toml(path: Path, cmd: str, args: list[str]) -> None:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    text = re.sub(
        r"(?ms)^\[mcp_servers\.codegraph\]\s*\n(?:(?!^\[).*\n?)*", "", text
    ).rstrip()
    block = (f"\n\n[mcp_servers.{SERVER_NAME}]\n"
             f"command = {json.dumps(cmd)}\n"
             f"args = {json.dumps(args)}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((text + block).lstrip("\n"), encoding="utf-8")


def _write_continue_yaml(path: Path, cmd: str, args: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    args_yaml = "".join(f"\n    - {json.dumps(a)}" for a in args)
    path.write_text(
        f"name: {SERVER_NAME}\ncommand: {json.dumps(cmd)}\nargs:{args_yaml}\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# instruction writers
# --------------------------------------------------------------------------- #

def _write_instr(spec: Instr, scope: str, root: Path) -> Path | None:
    # instructions are scope-specific — no user<->project fallback (a global
    # install must not drop an AGENTS.md into whatever directory you ran it from)
    fn = spec.user if scope == "user" else spec.project
    if fn is None:
        return None
    path = Path(fn() if scope == "user" else fn(root))
    path.parent.mkdir(parents=True, exist_ok=True)

    if spec.kind == "skill-md":
        src = resources.files("codegraph.skill") / "SKILL.md"
        shutil.copyfile(str(src), path)
    elif spec.kind == "commands-toml":
        body = _snippet_body().replace('"""', "'''")
        path.write_text(
            'description = "build / query the codegraph code graph"\n'
            f'prompt = """\n{body}\n\n'
            'User request: {{args}}\n'
            'If it is a "how does X / what uses X / what breaks if I change X" '
            'question, run the matching codegraph command and answer from its '
            'output. Otherwise build or update the graph and report the summary.\n'
            '"""\n',
            encoding="utf-8",
        )
    elif spec.kind == "agents-md":
        _upsert_markers(path, _snippet())
    else:
        return None
    return path


def _upsert_markers(path: Path, block: str) -> None:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _MARK_RE.search(existing):
        new = _MARK_RE.sub(block.strip(), existing)
    else:
        sep = "" if not existing or existing.endswith("\n\n") else (
            "\n" if existing.endswith("\n") else "\n\n")
        new = existing + sep + block
    path.write_text(new, encoding="utf-8")


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def install(agent_key: str, root: Path, *, scope: str = "global",
            do_mcp: bool = True, do_instructions: bool = True) -> list[dict]:
    agent = AGENTS.get(agent_key)
    if agent is None:
        raise ValueError(f"unknown agent '{agent_key}'; choose from "
                         f"{', '.join(a.key for a in MENU)}")
    scope = "user" if scope in ("global", "user") else "project"
    root = Path(root).resolve()
    out: list[dict] = []

    if agent.manual:
        out.append({"what": "manual", "path": agent.manual})
        return out

    if do_mcp and agent.mcp_format:
        res = _resolve_scoped(agent.mcp_user, agent.mcp_project, scope, root)
        if res:
            p = _write_mcp(agent, scope, root)
            if p:
                rec = {"what": "mcp", "path": str(p)}
                if res[1] != scope:
                    rec["note"] = f"{agent.label} MCP config is {res[1]}-scoped"
                out.append(rec)
    if do_instructions:
        for spec in agent.instr:
            p = _write_instr(spec, scope, root)
            if p:
                out.append({"what": spec.kind, "path": str(p)})
    return out
