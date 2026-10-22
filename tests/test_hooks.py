"""Tests for agent_undo.hooks."""

from __future__ import annotations

import re

from agent_undo import hooks


def test_get_session_id_creates_session(tmp_path, monkeypatch):
    """hooks.get_session_id() must create a session file, not return 'unknown'."""
    monkeypatch.chdir(tmp_path)
    session_id = hooks.get_session_id()
    assert session_id != "unknown"
    assert re.fullmatch(r"sess-[0-9a-f]{12}", session_id)
    sf = tmp_path / ".agent-undo" / "session"
    assert sf.exists()
    assert sf.read_text().strip() == session_id


def test_get_session_id_reuses_existing(tmp_path, monkeypatch):
    """If a session file already exists, return its ID."""
    monkeypatch.chdir(tmp_path)
    sf = tmp_path / ".agent-undo" / "session"
    sf.parent.mkdir(parents=True)
    sf.write_text("sess-abc123def456")
    assert hooks.get_session_id() == "sess-abc123def456"


def test_get_default_db_reflects_chdir(tmp_path, monkeypatch):
    """get_default_db() must resolve cwd at call time, not import time."""
    from agent_undo.hooks import get_default_db

    monkeypatch.chdir(tmp_path)
    db = get_default_db()
    assert db.parent == tmp_path / ".agent-undo"
    assert db.name == "journal.db"


def test_get_session_id_regenerates_empty_session_file(tmp_path, monkeypatch):
    """#77: an empty session file must not yield an empty (colliding) session ID."""
    monkeypatch.chdir(tmp_path)
    sf = tmp_path / ".agent-undo" / "session"
    sf.parent.mkdir(parents=True)
    sf.write_text("   \n")
    session_id = hooks.get_session_id()
    assert session_id
    assert re.fullmatch(r"sess-[0-9a-f]{12}", session_id)
    assert sf.read_text().strip() == session_id
