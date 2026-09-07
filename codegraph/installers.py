"""``codegraph install --platform <name>`` — register the MCP server with an
agent platform.

Every platform speaks MCP but keeps the config in a different file with a
slightly different shape. Each entry here knows (a) where the file lives,
(b) the JSON key the server list hangs off, and (c) whether that is a project
file or a user-global one. All writes are read-merge-write and idempotent.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Platform:
    name: str
    key: str                       # top-level key holding the server map
    scope: str                     # "project" | "user"
    # path resolver: (project_root) -> config file Path
    locate: object


def _appdata() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))


PLATFORMS: dict[str, Platform] = {
    "claude": Platform("claude", "mcpServers", "project",
                       lambda root: root / ".mcp.json"),
    "claude-desktop": Platform("claude-desktop", "mcpServers", "user",
                               lambda root: _appdata() / "Claude" / "claude_desktop_config.json"),
    "cursor": Platform("cursor", "mcpServers", "project",
                       lambda root: root / ".cursor" / "mcp.json"),
    "windsurf": Platform("windsurf", "mcpServers", "user",
                         lambda root: Path.home() / ".codeium" / "windsurf" / "mcp_config.json"),
    "vscode": Platform("vscode", "servers", "project",
                       lambda root: root / ".vscode" / "mcp.json"),
    "zed": Platform("zed", "context_servers", "user",
                    lambda root: _appdata() / "zed" / "settings.json"),
}


def server_entry(root: Path) -> dict:
    return {"command": sys.executable,
            "args": ["-m", "codegraph", "serve", str(root)]}


def install(platform: str, root: Path) -> dict:
    p = PLATFORMS.get(platform)
    if p is None:
        raise ValueError(f"unknown platform '{platform}'; "
                         f"choose from {', '.join(sorted(PLATFORMS))}")
    cfg = Path(p.locate(root))
    data: dict = {}
    if cfg.exists():
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    entry = server_entry(root)
    if p.name == "zed":
        # zed wants {"command": {"path":..., "args":...}} under context_servers
        entry = {"command": {"path": entry["command"], "args": entry["args"]},
                 "settings": {}}
    servers = data.setdefault(p.key, {})
    servers["codegraph"] = entry

    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return {"platform": p.name, "path": str(cfg), "scope": p.scope}
