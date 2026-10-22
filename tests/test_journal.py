"""Tests for agent-undo journal."""

from __future__ import annotations

import sqlite3

import pytest

from agent_undo.journal import Journal


@pytest.fixture
def journal(tmp_path):
    """Create a temporary journal."""
    return Journal(tmp_path / "test.db")


def test_record_and_retrieve(journal):
    op_id = journal.record("test-session", "file-write", path="/tmp/foo.txt")
    assert op_id > 0
    ops = journal.get_session_ops("test-session")
    assert len(ops) == 1
    assert ops[0]["path"] == "/tmp/foo.txt"


def test_record_preserves_zero_timestamp(journal):
    journal.record("test-session", "shell", command="x", timestamp=0)

    ops = journal.get_session_ops("test-session")

    assert ops[0]["timestamp"] == 0.0


def test_checkpoint(journal):
    journal.record("test-session", "file-write", path="/tmp/bar.txt")
    op_id = journal.checkpoint("test-session", "test-checkpoint")
    assert op_id > 0
    found = journal.find_checkpoint("test-session", "test-checkpoint")
    assert found == op_id


def test_timeline(journal):
    journal.record("test-session", "shell", command="echo hello")
    journal.record("test-session", "file-write", path="/tmp/x")
    journal.checkpoint("test-session", "cp1")
    timeline = journal.get_timeline("test-session")
    assert len(timeline) == 3


def test_rollback_ops_since(journal):
    journal.record("test-session", "file-write", path="/tmp/a")
    cp_id = journal.checkpoint("test-session", "before-change")
    journal.record("test-session", "file-write", path="/tmp/b")
    journal.record("test-session", "shell", command="rm -rf /tmp/c")
    ops = journal.get_ops_since("test-session", cp_id)
    # Returns only ops strictly AFTER the checkpoint (the checkpoint itself is just a marker)
    assert len(ops) == 2


def test_statistics(journal):
    journal.record("s1", "file-write")
    journal.record("s1", "shell")
    journal.record("s2", "file-write")
    stats = journal.get_statistics()
    assert stats["total_operations"] == 3
    assert stats["total_sessions"] == 2
    assert stats["by_type"]["file-write"] == 2


def test_git_classification_from_json_tool_input(tmp_path, monkeypatch):
    """Bug #55: Claude Code passes tool_input as JSON, not raw string."""
    import json

    from agent_undo.hooks import get_session_id, run_pre_tool_hook

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("AGENT_UNDO_DB", str(db_path))
    monkeypatch.chdir(tmp_path)

    git_input = json.dumps({"command": "git commit -m 'test'"})
    run_pre_tool_hook("Bash", git_input)

    from agent_undo.journal import Journal

    journal = Journal(db_path)
    session_id = get_session_id()
    ops = journal.get_session_ops(session_id)
    assert len(ops) == 1
    assert ops[0]["op_type"] == "git"
    assert ops[0]["command"] == "git commit -m 'test'"


def test_journal_close_releases_connection(journal):
    """#76: close() must actually close the underlying SQLite handle."""
    conn = journal._conn
    journal.close()
    assert journal._conn is None
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")


def test_journal_close_is_idempotent(journal):
    journal.close()
    journal.close()  # must not raise


def test_journal_context_manager_closes(tmp_path):
    with Journal(tmp_path / "cm.db") as j:
        j.record("s", "shell", command="echo hi")
        assert j._conn is not None
    assert j._conn is None


def test_journal_reopen_after_close(tmp_path):
    j = Journal(tmp_path / "re.db")
    j.record("s", "shell", command="a")
    j.close()
    j2 = Journal(tmp_path / "re.db")
    assert len(j2.get_session_ops("s")) == 1
    j2.close()
