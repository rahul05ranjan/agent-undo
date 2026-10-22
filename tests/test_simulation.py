"""Tests for agent-undo rollback simulation."""

from __future__ import annotations

import pytest

from agent_undo.journal import Journal
from agent_undo.rollback import (
    RollbackGenerator,
    RollbackOperation,
    RollbackPlan,
    format_simulation,
    simulate_rollback,
)


@pytest.fixture
def journal(tmp_path):
    return Journal(tmp_path / "test.db")


@pytest.fixture
def generator(journal):
    return RollbackGenerator(journal)


class TestRollbackPlan:
    def test_empty_plan(self):
        plan = RollbackPlan(session_id="s1", target_op_id=1, operations=[])
        assert len(plan.operations) == 0
        assert plan.session_id == "s1"

    def test_plan_with_operations(self):
        ops = [
            RollbackOperation(
                op_type="file-write", path="/tmp/a.txt", content_before="old content"
            ),
            RollbackOperation(op_type="shell", command="echo hello"),
        ]
        plan = RollbackPlan(session_id="s1", target_op_id=1, operations=ops)
        assert len(plan.operations) == 2


class TestRollbackOperation:
    def test_file_write_op(self):
        op = RollbackOperation(
            op_type="file-write",
            path="/tmp/test.txt",
            content_before="original content",
        )
        assert op.op_type == "file-write"
        assert op.path == "/tmp/test.txt"
        assert op.content_before == "original content"

    def test_shell_op(self):
        op = RollbackOperation(op_type="shell", command="rm -rf /tmp/x")
        assert op.op_type == "shell"
        assert op.command == "rm -rf /tmp/x"


class TestGeneratePlan:
    def test_plan_from_checkpoint(self, journal, generator):
        session_id = "test-sess"
        journal.record(session_id, "file-write", path="/tmp/a.txt", content_before="original")
        cp_id = journal.checkpoint(session_id, "cp1")
        journal.record(session_id, "file-write", path="/tmp/b.txt", content_before="original b")
        journal.record(session_id, "shell", command="echo done")

        plan = generator.generate_plan(session_id, checkpoint_label="cp1")

        assert plan.session_id == session_id
        assert plan.target_op_id == cp_id
        assert len(plan.operations) == 2  # shell + file-write (not the checkpoint)

    def test_plan_empty_when_no_ops_since(self, journal, generator):
        session_id = "test-sess"
        journal.checkpoint(session_id, "cp1")

        plan = generator.generate_plan(session_id, checkpoint_label="cp1")
        assert len(plan.operations) == 0

    def test_plan_none_when_missing_checkpoint(self, journal, generator):
        with pytest.raises(ValueError, match="not found"):
            generator.generate_plan("s1", checkpoint_label="nonexistent")


class TestFormatSimulation:
    def test_empty_plan_simulation(self):
        plan = RollbackPlan(session_id="s1", target_op_id=1, operations=[])
        output = format_simulation(plan)
        assert "=== Rollback Simulation ===" in output
        assert "Operations to undo: 0" in output
        assert "No files, git repos, or the journal have been modified" in output

    def test_file_write_restoring(self, generator, journal):
        session_id = "test-sess"
        journal.checkpoint(session_id, "cp1")
        # Insert ops AFTER the checkpoint
        journal.record(
            session_id, "file-write", path="/tmp/config.py", content_before="DEBUG = False\n"
        )
        journal.record(session_id, "file-write", path="/tmp/b.txt", content_before="original b")

        plan = generator.generate_plan(session_id, checkpoint_label="cp1")
        output = format_simulation(plan)

        assert "[RESTORE] /tmp/config.py" in output
        assert "[RESTORE] /tmp/b.txt" in output
        assert "DEBUG = False" in output

    def test_shell_command_included(self, generator, journal):
        session_id = "test-sess"
        journal.checkpoint(session_id, "cp1")
        # Insert ops AFTER the checkpoint
        journal.record(session_id, "shell", command="echo hello")
        journal.record(session_id, "shell", command="rm -rf /tmp/x")

        plan = generator.generate_plan(session_id, checkpoint_label="cp1")
        output = format_simulation(plan)

        assert "Shell commands that will be executed" in output
        assert "# Would execute undo for: echo hello" in output
        assert "# Would execute undo for: rm -rf /tmp/x" in output

    def test_git_commit_undo(self, generator, journal):
        session_id = "test-sess"
        journal.checkpoint(session_id, "cp1")
        # Insert ops AFTER the checkpoint
        journal.record(session_id, "git", command="git commit -m 'feat: add stuff'")
        journal.record(session_id, "git", command="git push origin main")

        plan = generator.generate_plan(session_id, checkpoint_label="cp1")
        output = format_simulation(plan)

        assert "git reset --soft HEAD~1" in output
        assert "Remote push cannot be auto-undone" in output

    def test_simulation_contains_footer(self, generator, journal):
        session_id = "test-sess"
        journal.checkpoint(session_id, "cp1")
        # Insert ops AFTER the checkpoint
        journal.record(session_id, "file-write", path="/tmp/x", content_before="data")
        journal.record(session_id, "shell", command="echo test")

        output = simulate_rollback(generator, session_id, checkpoint_label="cp1")

        assert "=== Rollback Simulation ===" in output
        assert "NOTE: This is a simulation." in output
        assert "[RESTORE] /tmp/x" in output

    def test_deterministic_output(self, generator, journal):
        session_id = "test-sess"
        journal.record(session_id, "file-write", path="/tmp/a", content_before="line1\nline2")
        journal.checkpoint(session_id, "cp1")

        output1 = simulate_rollback(generator, session_id, checkpoint_label="cp1")
        output2 = simulate_rollback(generator, session_id, checkpoint_label="cp1")

        assert output1 == output2


class TestDiffPreviewLineNumbers:
    """Tests for diff preview line number fix — bug fix for issue #62."""

    def test_diff_preview_uses_real_line_numbers(self, generator, journal):
        """Diff preview must use real line numbers, not literal '@@ -1,N +1,N @@'."""
        session_id = "test-sess"
        journal.checkpoint(session_id, "cp1")
        content = "line1\nline2\nline3\nline4\nline5"
        journal.record(session_id, "file-write", path="/tmp/test.txt", content_before=content)

        plan = generator.generate_plan(session_id, checkpoint_label="cp1")
        output = format_simulation(plan)

        # Should NOT contain the literal placeholder
        assert "@@ -1,N +1,N @@" not in output
        # Should contain real line numbers (5 lines)
        assert "@@ -1,5 +1,5 @@" in output

    def test_diff_preview_single_line_file(self, generator, journal):
        """Single-line file should show @@ -1,1 +1,1 @@."""
        session_id = "test-sess"
        journal.checkpoint(session_id, "cp1")
        journal.record(session_id, "file-write", path="/tmp/single.txt", content_before="only line")

        plan = generator.generate_plan(session_id, checkpoint_label="cp1")
        output = format_simulation(plan)

        assert "@@ -1,1 +1,1 @@" in output

    def test_diff_preview_empty_file(self, generator, journal):
        """Empty file should show @@ -0,0 +1,0 @@ or similar."""
        session_id = "test-sess"
        journal.checkpoint(session_id, "cp1")
        journal.record(session_id, "file-write", path="/tmp/empty.txt", content_before="")

        plan = generator.generate_plan(session_id, checkpoint_label="cp1")
        output = format_simulation(plan)

        # Empty content has 0 lines
        assert "@@ -1,N +1,N @@" not in output


class TestRollbackWithoutCheckpoint:
    """Tests for rollback --dry-run without checkpoint — bug fix for issue #61."""

    def test_dry_run_without_checkpoint_shows_all_operations(
        self, generator, journal, capsys, tmp_path, monkeypatch
    ):
        """rollback --dry-run without --checkpoint should show all operations."""
        session_id = "test-sess"
        journal.record(session_id, "file-write", path="/tmp/a.txt", content_before="original a")
        journal.record(session_id, "shell", command="echo hello")
        journal.record(session_id, "file-write", path="/tmp/b.txt", content_before="original b")

        # Set up session file so get_session_id returns the same session
        session_dir = tmp_path / ".agent-undo"
        session_dir.mkdir(exist_ok=True)
        (session_dir / "session").write_text(session_id)
        monkeypatch.chdir(tmp_path)

        from agent_undo.cli import cmd_rollback

        db_path = str(journal.db_path)
        result = cmd_rollback([db_path, "--dry-run"])

        captured = capsys.readouterr()
        assert result == 0
        assert "=== Rollback Simulation ===" in captured.out
        assert "[RESTORE] /tmp/a.txt" in captured.out
        assert "[RESTORE] /tmp/b.txt" in captured.out
        assert "echo hello" in captured.out

    def test_dry_run_without_checkpoint_no_operations(self, journal, capsys, tmp_path, monkeypatch):
        """rollback --dry-run without operations should show a clear message."""
        session_id = "test-sess"
        session_dir = tmp_path / ".agent-undo"
        session_dir.mkdir(exist_ok=True)
        (session_dir / "session").write_text(session_id)
        monkeypatch.chdir(tmp_path)

        from agent_undo.cli import cmd_rollback

        db_path = str(journal.db_path)
        result = cmd_rollback([db_path, "--dry-run"])

        captured = capsys.readouterr()
        assert result == 0
        assert "No operations recorded yet." in captured.out

    def test_dry_run_with_checkpoint_still_works(
        self, generator, journal, capsys, tmp_path, monkeypatch
    ):
        """rollback --dry-run with --checkpoint should still work as before."""
        session_id = "test-sess"
        journal.checkpoint(session_id, "cp1")
        journal.record(session_id, "file-write", path="/tmp/a.txt", content_before="original")

        session_dir = tmp_path / ".agent-undo"
        session_dir.mkdir(exist_ok=True)
        (session_dir / "session").write_text(session_id)
        monkeypatch.chdir(tmp_path)

        from agent_undo.cli import cmd_rollback

        db_path = str(journal.db_path)
        result = cmd_rollback([db_path, "--dry-run", "cp1"])

        captured = capsys.readouterr()
        assert result == 0
        assert "=== Rollback Simulation ===" in captured.out
        assert "[RESTORE] /tmp/a.txt" in captured.out
