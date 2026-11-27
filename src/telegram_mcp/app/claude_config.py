"""Registering the server with Claude Desktop and Claude Code, so nobody edits JSON by hand.

Claude Desktop keeps its config in different places depending on how it was
installed (the regular installer, the Microsoft Store package, managed builds),
so every place that exists is updated. Claude Code is told through its own CLI,
which owns ~/.claude.json and rewrites it while running.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SERVER_NAME = "telegram"
CONFIG_NAME = "claude_desktop_config.json"


def server_command() -> list[str]:
    """How Claude should start this server: the installed program, or this Python."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "serve"]
    return [sys.executable, "-m", "telegram_mcp", "serve"]


def desktop_config_paths() -> list[Path]:
    """Every Claude Desktop config this user has, plus where a fresh one should go."""
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Claude" / CONFIG_NAME]
    if sys.platform != "win32":
        return [home / ".config" / "Claude" / CONFIG_NAME]

    found = {appdata / "Claude" / CONFIG_NAME}
    # Microsoft Store builds see %APPDATA% redirected into their package folder.
    for package in local.glob("Packages/Claude_*"):
        found.add(package / "LocalCache" / "Roaming" / "Claude" / CONFIG_NAME)
        found.update(package.glob(f"LocalCache/Roaming/Claude*/{CONFIG_NAME}"))
    found.update(local.glob(f"Claude-Data/*/{CONFIG_NAME}"))
    return sorted(found)


def _wanted_entry() -> dict[str, Any]:
    command = server_command()
    return {"command": command[0], "args": command[1:]}


@dataclass
class DesktopTarget:
    path: Path
    exists: bool
    registered: bool


def desktop_status() -> list[DesktopTarget]:
    wanted = _wanted_entry()
    targets = []
    for path in desktop_config_paths():
        exists = path.exists()
        registered = False
        if exists:
            try:
                data = json.loads(path.read_text(encoding="utf-8") or "{}")
                registered = data.get("mcpServers", {}).get(SERVER_NAME) == wanted
            except (OSError, ValueError, AttributeError):
                registered = False
        targets.append(DesktopTarget(path, exists, registered))
    return targets


def register_desktop() -> list[Path]:
    """Add or update our entry in every existing config; create one if there is none."""
    targets = desktop_status()
    existing = [t.path for t in targets if t.exists]
    chosen = existing or [t.path for t in targets if t.path.parent.parent.exists()]
    if not chosen:
        chosen = [targets[0].path]
    for path in chosen:
        data: dict[str, Any] = {}
        if path.exists():
            text = path.read_text(encoding="utf-8")
            data = json.loads(text) if text.strip() else {}
            if not isinstance(data, dict):
                raise ValueError(f"{path} is not a JSON object; fix or delete it first.")
            path.with_suffix(".json.bak").write_text(text, encoding="utf-8")
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            servers = {}
        servers[SERVER_NAME] = _wanted_entry()
        data["mcpServers"] = servers
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return chosen


def find_claude_code() -> str | None:
    return shutil.which("claude")


def _run_claude(args: list[str]) -> subprocess.CompletedProcess[str]:
    exe = find_claude_code()
    if exe is None:
        raise FileNotFoundError("claude")
    # npm installs claude as a .cmd/.ps1 shim on Windows; cmd.exe runs either form.
    if sys.platform == "win32" and not exe.lower().endswith(".exe"):
        cmd = ["cmd.exe", "/c", exe.removesuffix(".ps1") if exe.endswith(".ps1") else exe]
    else:
        cmd = [exe]
    return subprocess.run(
        [*cmd, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def code_registered() -> bool | None:
    """True/False if Claude Code is installed, None if it is not."""
    if find_claude_code() is None:
        return None
    # Reading is safe while Claude Code runs; only writing goes through its CLI.
    # (`claude mcp get` would be exact but starts every server to health-check it.)
    try:
        data = json.loads((Path.home() / ".claude.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    entry = data.get("mcpServers", {}).get(SERVER_NAME) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return False
    return [entry.get("command"), *entry.get("args", [])] == server_command()


def register_code() -> bool:
    """Register with Claude Code for all projects. False if Claude Code is not installed."""
    if find_claude_code() is None:
        return False
    _run_claude(["mcp", "remove", SERVER_NAME, "-s", "user"])
    result = _run_claude(["mcp", "add", SERVER_NAME, "--scope", "user", "--", *server_command()])
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:300])
    return True
