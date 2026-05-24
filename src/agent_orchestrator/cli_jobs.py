"""Job command handlers for the Agent Orchestrator CLI."""
from __future__ import annotations

# DEPS: __future__, agent_orchestrator, argparse, pathlib
# RESPONSIBILITY: Execute job status, result, send, and cancel CLI commands.
# MODULE: interface
# ---

import argparse
from pathlib import Path

from agent_orchestrator.cli_common import emit_json
from agent_orchestrator.command import CommandJobRuntime


def run_job_command(args: argparse.Namespace) -> bool:
    if args.command == "status":
        runtime = CommandJobRuntime(root=Path(args.root))
        payload = runtime.status(args.job_id).to_dict()
        emit_json(payload, args, summary=lambda: print_job_cli_summary("status", payload))
        return True

    if args.command == "result":
        runtime = CommandJobRuntime(root=Path(args.root))
        payload = runtime.result(args.job_id).to_dict()
        emit_json(payload, args, summary=lambda: print_job_cli_summary("result", payload))
        return True

    if args.command == "send":
        runtime = CommandJobRuntime(root=Path(args.root))
        payload = runtime.send(args.job_id, args.message).to_dict()
        emit_json(payload, args, summary=lambda: print_job_cli_summary("send", payload))
        return True

    if args.command == "cancel":
        runtime = CommandJobRuntime(root=Path(args.root))
        payload = runtime.cancel(args.job_id).to_dict()
        emit_json(payload, args, summary=lambda: print_job_cli_summary("cancel", payload))
        return True

    return False


def print_job_cli_summary(command: str, payload: dict[str, object]) -> None:
    job_id = payload.get("id") or payload.get("job_id") or "unknown"
    status = payload.get("status") or "unknown"
    phase = payload.get("phase") or "unknown"
    summary = payload.get("summary") or payload.get("error") or ""
    parsed = payload.get("parsed_payload", {}) if isinstance(payload.get("parsed_payload"), dict) else {}
    operation = parsed.get("operation", {}) if isinstance(parsed.get("operation"), dict) else {}
    operation_suffix = ""
    if operation:
        operation_suffix = f" operation={operation.get('status', 'unknown')} reason={operation.get('reason', 'unknown')}"
    terminal_ref = None
    metadata = payload.get("metadata", {}) if isinstance(payload.get("metadata"), dict) else {}
    if metadata:
        terminal_ref = metadata.get("terminal_ref")
    suffix = f" terminal={terminal_ref}" if terminal_ref else ""
    last_seen = payload.get("updated_at") or payload.get("completed_at") or payload.get("started_at") or "unknown"
    stdout = str(payload.get("stdout") or payload.get("raw_output") or "")
    excerpt = " ".join(stdout.split())[:120] if stdout else ""
    print(f"job_{command}: id={job_id} status={status} phase={phase}{suffix}{operation_suffix} last_seen={last_seen} summary={summary}")
    if excerpt:
        print(f"job_log_excerpt: {excerpt}")
