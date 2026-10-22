"""Regression tests for CLI argument parsing (issues #53, #56, #57)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from agent_undo.cli import main


def _rows(db) -> list[tuple]:
    con = sqlite3.connect(db)
    try:
        return con.execute(
            'SELECT o.id, o.op_type, o."command", o.path, c.label '
            "FROM operations o LEFT JOIN checkpoints c ON c.operation_id = o.id"
        ).fetchall()
    finally:
        con.close()


def test_log_flags_write_to_the_real_journal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["log", "--type", "shell", "--cmd", "npm test", "--exit", "0"]) == 0

    assert not (tmp_path / "--type").exists(), "flag consumed as a filesystem path"
    assert not (tmp_path / "--cmd").exists()
    rows = _rows(tmp_path / ".agent-undo" / "journal.db")
    assert ("npm test",) in [(r[2],) for r in rows]


def test_log_rejects_an_unknown_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    with pytest.raises(SystemExit) as exc:
        main(["log", "--bogus"])
    assert exc.value.code == 2


def test_log_requires_a_target_for_targeted_types(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["log", "--type", "shell"]) == 2
    assert "requires --command" in capsys.readouterr().err


def test_log_still_accepts_positional_db_and_type(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENT_UNDO_COMMAND", "ls -la")
    other = tmp_path / "other.db"
    assert main(["log", str(other), "shell"]) == 0
    assert _rows(other)


def test_rollback_dry_run_does_not_create_a_stray_db(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["rollback", "--dry-run"]) == 0
    assert not (tmp_path / "--dry-run").exists()
    assert (tmp_path / ".agent-undo" / "journal.db").exists()


def test_rollback_accepts_the_checkpoint_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["checkpoint", "before-refactor"]) == 0
    assert main(["rollback", "--to", "checkpoint:before-refactor", "--dry-run"]) == 0


def test_rollback_unknown_checkpoint_is_a_clean_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["rollback", "--to", "checkpoint:nope"]) == 1
    err = capsys.readouterr().err
    assert "not found" in err
    assert "Traceback" not in err


def test_rollback_writes_the_requested_output_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["log", "--type", "shell", "--cmd", "npm test"]) == 0
    assert main(["rollback", "--output", "out.sh"]) == 0
    assert (tmp_path / "out.sh").exists()
    assert not (tmp_path / "--output").exists()


def test_record_pre_tool_hook_is_journaled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    payload = json.dumps({"command": "git commit -m 'refactor auth'"})
    assert main(["record", "--tool", "Bash", "--input", payload]) == 0

    rows = _rows(tmp_path / ".agent-undo" / "journal.db")
    assert any(r[1] == "git" and r[2] == "git commit -m 'refactor auth'" for r in rows)


def test_record_post_tool_hook_is_journaled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["record", "--tool", "Bash", "--output", "ok", "--exit", "0"]) == 0

    rows = _rows(tmp_path / ".agent-undo" / "journal.db")
    assert any(r[1] == "tool-result" for r in rows)


def test_wrap_records_the_wrapped_command(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["wrap", "--", "true"]) == 0

    rows = _rows(tmp_path / ".agent-undo" / "journal.db")
    assert any(r[1] == "shell" and r[2] == "true" for r in rows)
    assert any(r[1] == "checkpoint" and r[4] == "wrap-start" for r in rows)


def test_wrap_without_a_command_is_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init"]) == 0
    assert main(["wrap"]) == 2
    assert "requires a command" in capsys.readouterr().err
