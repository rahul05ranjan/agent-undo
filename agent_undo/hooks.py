"""Agent hook integrations for Claude Code, Codex, and shell-level recording."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

from .journal import Journal

def get_default_db() -> Path:
    """Resolve the default DB path at call time, not import time."""
    return Path.cwd() / ".agent-undo" / "journal.db"


def get_session_file() -> Path:
    """Get the session file path."""
    return Path.cwd() / ".agent-undo" / "session"


def get_session_id() -> str:
    """Get or create a session ID for this agent-undo session."""
    sf = get_session_file()
    if sf.exists():
        return sf.read_text().strip()
    session_id = f"sess-{uuid.uuid4().hex[:12]}"
    sf.parent.mkdir(parents=True, exist_ok=True)
    sf.write_text(session_id)
    return session_id


def run_pre_tool_hook(tool_name: str, tool_input: str) -> None:
    """Record a tool use BEFORE it executes (Claude Code preToolUse hook)."""
    db_path = Path(os.environ.get("AGENT_UNDO_DB", str(get_default_db())))
    journal = Journal(db_path)
    session_id = get_session_id()

    path = ""
    if tool_name == "Bash" and tool_input.startswith("git "):
        op_type = "git"
    elif tool_name == "Write" or tool_name == "Edit":
        op_type = "file-write"
        # Try to extract path from input
        try:
            data = json.loads(tool_input)
            path = data.get("file_path", "")
        except (json.JSONDecodeError, TypeError):
            path = ""
    else:
        op_type = "shell"

    # Read current file content before modification
    content_before = None
    if path and op_type == "file-write" and Path(path).exists():
        try:
            content_before = Path(path).read_text()
        except (OSError, UnicodeDecodeError):
            pass

    journal.record(
        session_id,
        op_type,
        command=tool_input,
        path=path or None,
        content_before=content_before,
    )


def run_post_tool_hook(tool_name: str, tool_output: str, exit_code: int) -> None:
    """Record a tool use AFTER it executes (Claude Code postToolUse hook)."""
    db_path = Path(os.environ.get("AGENT_UNDO_DB", str(get_default_db())))
    journal = Journal(db_path)
    session_id = get_session_id()

    journal.record(
        session_id,
        "tool-result",
        command=f"{tool_name} (exit={exit_code})",
        exit_code=exit_code,
        metadata={"tool": tool_name, "output_length": len(tool_output)},
    )


def shell_wrapper(agent_cmd: list[str], db_path: Path | None = None) -> int:
    """Run an agent command with shell-level operation recording."""
    db = db_path or get_default_db()
    journal = Journal(db)
    session_id = get_session_id()

    journal.checkpoint(session_id, "wrap-start")

    import shlex

    cmd_str = " ".join(shlex.quote(c) for c in agent_cmd)

    journal.record(
        session_id,
        "shell",
        command=cmd_str,
        metadata={"wrapped": True, "argv": agent_cmd},
    )

    try:
        result = subprocess.run(agent_cmd)
        exit_code = result.returncode
    except FileNotFoundError:
        print(f"Command not found: {agent_cmd[0]}", file=sys.stderr)
        exit_code = 127
    except KeyboardInterrupt:
        exit_code = 130

    journal.record(
        session_id,
        "shell-result",
        command=f"{cmd_str} (exit={exit_code})",
        exit_code=exit_code,
    )

    journal.checkpoint(session_id, "wrap-end")
    return exit_code
