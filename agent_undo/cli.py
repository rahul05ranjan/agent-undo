"""CLI entry point for agent-undo."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from .journal import Journal
from .rollback import RollbackGenerator, preview_rollback, simulate_rollback


def get_default_db() -> Path:
    """Resolve the default DB path at call time, not import time."""
    return Path.cwd() / ".agent-undo" / "journal.db"


def get_session_id() -> str:
    """Get or create a session ID for this agent-undo session."""
    session_file = Path.cwd() / ".agent-undo" / "session"
    if session_file.exists():
        return session_file.read_text().strip()
    session_id = f"sess-{uuid.uuid4().hex[:12]}"
    session_file.parent.mkdir(parents=True, exist_ok=True)
    session_file.write_text(session_id)
    return session_id


def cmd_init(args: list[str]) -> int:
    """Initialize agent-undo in the current project."""
    db_path = Path(args[0]) if args else get_default_db()
    journal = Journal(db_path)
    session_id = get_session_id()
    journal.checkpoint(session_id, "session-start")
    print(f"agent-undo initialized — session: {session_id}")
    print(f"Journal: {db_path}")
    return 0


def cmd_log(args: list[str]) -> int:
    """Log an operation manually."""
    db_path = Path(args[0]) if args else get_default_db()
    op_type = args[1] if len(args) > 1 else "manual"

    journal = Journal(db_path)
    session_id = get_session_id()

    command = os.environ.get("AGENT_UNDO_COMMAND")
    path = os.environ.get("AGENT_UNDO_PATH")
    exit_code = int(os.environ.get("AGENT_UNDO_EXIT_CODE", "0"))

    op_id = journal.record(
        session_id,
        op_type,
        command=command,
        path=path,
        exit_code=exit_code,
    )
    print(f"Recorded {op_type} (id={op_id})")
    return 0


def cmd_timeline(args: list[str]) -> int:
    """Show the operation timeline."""
    db_path = Path(args[0]) if args else get_default_db()
    journal = Journal(db_path)
    session_id = get_session_id()

    timeline = journal.get_timeline(session_id)
    if not timeline:
        print("No operations recorded yet.")
        return 0

    for op in timeline:
        ts = op["timestamp"]
        op_type = op["op_type"]
        label = op.get("checkpoint_label") or ""
        cmd = op.get("command") or op.get("path") or ""
        print(f"  {ts:>12.2f}  {op_type:<16} {label:<20} {cmd}")
    return 0


def cmd_checkpoint(args: list[str]) -> int:
    """Create a named checkpoint."""
    db_path = Path(args[0]) if args else get_default_db()
    label = args[1] if len(args) > 1 else "checkpoint"

    journal = Journal(db_path)
    session_id = get_session_id()
    op_id = journal.checkpoint(session_id, label)
    print(f"Checkpoint '{label}' created (id={op_id})")
    return 0


def cmd_rollback(args: list[str]) -> int:
    """Generate a rollback script or show simulation."""
    db_path = Path(args[0]) if args else get_default_db()
    checkpoint = None
    dry_run = False
    for arg in args[1:]:
        if arg == "--dry-run":
            dry_run = True
        elif checkpoint is None:
            checkpoint = arg

    journal = Journal(db_path)
    session_id = get_session_id()

    if dry_run:
        gen = RollbackGenerator(journal)
        if checkpoint is None:
            # Show all operations since the start of the session
            ops = journal.get_session_ops(session_id)
            if not ops:
                print("No operations recorded yet.")
                return 0
            # Build a plan from all operations (excluding checkpoints)
            from .rollback import RollbackOperation, RollbackPlan, format_simulation
            operations = [
                RollbackOperation(
                    op_type=op["op_type"],
                    path=op.get("path"),
                    command=op.get("command"),
                    label=op.get("checkpoint_label", ""),
                    content_before=op.get("content_before"),
                )
                for op in ops
                if op["op_type"] != "checkpoint"
            ]
            plan = RollbackPlan(
                session_id=session_id,
                target_op_id=0,
                operations=operations,
            )
            print(format_simulation(plan))
        else:
            print(simulate_rollback(gen, session_id, checkpoint_label=checkpoint))
        return 0

    gen = RollbackGenerator(journal)
    if checkpoint is None:
        # Generate script for all operations since start
        ops = journal.get_session_ops(session_id)
        if not ops:
            print("No operations to undo.")
            return 0
        # Use the first operation as target (undo everything after it)
        first_op_id = ops[0]["id"]
        script = gen.generate(session_id, target_op_id=first_op_id)
    else:
        script = gen.generate(session_id, checkpoint_label=checkpoint)

    output = "undo.sh"
    Path(output).write_text(script)
    os.chmod(output, 0o755)
    print(f"Rollback script written to {output}")
    return 0


def cmd_preview(args: list[str]) -> int:
    """Preview a rollback."""
    db_path = Path(args[0]) if args else get_default_db()
    checkpoint = args[1] if len(args) > 1 else None

    journal = Journal(db_path)
    session_id = get_session_id()

    print(preview_rollback(journal, session_id, checkpoint_label=checkpoint))
    return 0


def cmd_stats(args: list[str]) -> int:
    """Show journal statistics."""
    db_path = Path(args[0]) if args else get_default_db()
    journal = Journal(db_path)
    stats = journal.get_statistics()
    print(json.dumps(stats, indent=2))
    return 0


COMMANDS = {
    "init": cmd_init,
    "log": cmd_log,
    "timeline": cmd_timeline,
    "checkpoint": cmd_checkpoint,
    "rollback": cmd_rollback,
    "preview": cmd_preview,
    "stats": cmd_stats,
}


def main(argv: list[str] | None = None) -> int:
    """Main CLI entry point."""
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] not in COMMANDS:
        print("Usage: agent-undo <command> [args...]")
        print(f"Commands: {', '.join(COMMANDS.keys())}")
        return 1

    return COMMANDS[argv[0]](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
