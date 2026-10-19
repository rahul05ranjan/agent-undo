"""CLI entry point for agent-undo."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path

from .journal import Journal
from .rollback import RollbackGenerator, preview_rollback, simulate_rollback

# Each operation type that cannot be rolled back without a recorded target.
REQUIRED_FIELD: dict[str, str] = {
    "shell": "command",
    "git": "command",
    "file-write": "path",
}


def get_default_db() -> Path:
    """Resolve the default DB path at call time, not import time."""
    return Path.cwd() / ".agent-undo" / "journal.db"


def _db_path(args: list[str]) -> Path:
    """Resolve the journal path from the first positional arg.

    A token starting with ``--`` is a flag, never a filesystem path: consuming
    it as the DB path silently writes the journal to a stray file (issues
    #53 and #56). Callers that need flag semantics parse their own args.
    """
    if args and not args[0].startswith("-"):
        return Path(args[0])
    return get_default_db()


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
    db_path = _db_path(args)
    journal = Journal(db_path)
    session_id = get_session_id()
    journal.checkpoint(session_id, "session-start")
    print(f"agent-undo initialized — session: {session_id}")
    print(f"Journal: {db_path}")
    return 0


def cmd_log(args: list[str]) -> int:
    """Log an operation manually.

    Flags are parsed explicitly; a stray ``--`` token can never be consumed as
    the database path, and an operation type that needs a recorded target
    refuses to write a row without it.
    """
    parser = argparse.ArgumentParser(prog="agent-undo log")
    parser.add_argument("db_path", nargs="?")
    parser.add_argument("op_type_pos", nargs="?")
    parser.add_argument("--type", dest="type_opt")
    parser.add_argument("--cmd")
    parser.add_argument("--path")
    parser.add_argument("--content")
    parser.add_argument("--exit", dest="exit_code", type=int)
    parsed = parser.parse_args(args)

    db_path = Path(parsed.db_path) if parsed.db_path else get_default_db()
    op_type = parsed.type_opt or parsed.op_type_pos or "manual"

    command = parsed.cmd or os.environ.get("AGENT_UNDO_COMMAND")
    path = parsed.path or os.environ.get("AGENT_UNDO_PATH")
    content_before = parsed.content
    exit_code = (
        parsed.exit_code
        if parsed.exit_code is not None
        else int(os.environ.get("AGENT_UNDO_EXIT_CODE", "0"))
    )

    required = REQUIRED_FIELD.get(op_type)
    if required and not {"command": command, "path": path}.get(required):
        print(f"agent-undo: {op_type} requires --{required}", file=sys.stderr)
        return 2

    journal = Journal(db_path)
    session_id = get_session_id()
    op_id = journal.record(
        session_id,
        op_type,
        command=command,
        path=path,
        content_before=content_before,
        exit_code=exit_code,
    )
    print(f"Recorded {op_type} (id={op_id})")
    return 0


def cmd_timeline(args: list[str]) -> int:
    """Show the operation timeline."""
    db_path = _db_path(args)
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
    parser = argparse.ArgumentParser(prog="agent-undo checkpoint")
    parser.add_argument("label_pos", nargs="?")
    parser.add_argument("--label", dest="label_opt")
    parser.add_argument("--db")
    parsed = parser.parse_args(args)

    db_path = Path(parsed.db) if parsed.db else get_default_db()
    label = parsed.label_opt or parsed.label_pos or "checkpoint"

    journal = Journal(db_path)
    session_id = get_session_id()
    op_id = journal.checkpoint(session_id, label)
    print(f"Checkpoint '{label}' created (id={op_id})")
    return 0


def cmd_rollback(args: list[str]) -> int:
    """Generate a rollback script or show simulation."""
    parser = argparse.ArgumentParser(prog="agent-undo rollback")
    parser.add_argument("db_path", nargs="?")
    parser.add_argument("checkpoint_pos", nargs="?")
    parser.add_argument("--to")
    parser.add_argument("--output", default="undo.sh")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parsed, unknown = parser.parse_known_args(args)

    # Python 3.10/3.11 argparse rejects positional values that follow optionals
    # (e.g. ["db", "--dry-run", "cp1"]) because "cp1" looks like an unknown option.
    # parse_known_args collects them in `unknown`; treat the first as checkpoint_pos.
    if unknown and not parsed.checkpoint_pos:
        parsed.checkpoint_pos = unknown[0]

    db_path = Path(parsed.db_path) if parsed.db_path else get_default_db()
    checkpoint = parsed.to or parsed.checkpoint_pos
    if checkpoint and checkpoint.startswith("checkpoint:"):
        checkpoint = checkpoint[len("checkpoint:"):]
    dry_run = parsed.dry_run
    output = parsed.output

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
        try:
            script = gen.generate(session_id, target_op_id=first_op_id)
        except ValueError as exc:
            print(f"agent-undo: {exc}", file=sys.stderr)
            return 1
    else:
        try:
            script = gen.generate(session_id, checkpoint_label=checkpoint)
        except ValueError as exc:
            print(f"agent-undo: {exc}", file=sys.stderr)
            return 1

    Path(output).write_text(script)
    os.chmod(output, 0o755)
    print(f"Rollback script written to {output}")
    return 0


def cmd_preview(args: list[str]) -> int:
    """Preview a rollback."""
    db_path = _db_path(args)
    checkpoint = args[1] if len(args) > 1 else None
    if checkpoint and checkpoint.startswith("checkpoint:"):
        checkpoint = checkpoint[len("checkpoint:"):]

    journal = Journal(db_path)
    session_id = get_session_id()

    try:
        print(preview_rollback(journal, session_id, checkpoint_label=checkpoint))
    except ValueError as exc:
        print(f"agent-undo: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_stats(args: list[str]) -> int:
    """Show journal statistics."""
    db_path = _db_path(args)
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
