"""ID、版本和操作收据一致的记忆管理命令。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3

from nz_coder.protocol.public_error import to_public_error
from nz_coder.state.memory import current_memory_manager
from nz_coder.state.memory_control import MemoryControlPlane


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nz-coder memory")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "list", "pending", "ledger"):
        sub = commands.add_parser(name)
        sub.add_argument("--json", action="store_true")
        if name in {"list", "pending", "ledger"}:
            sub.add_argument("--limit", type=int, default=50)
        if name == "list":
            sub.add_argument("--type", choices=("user", "project", "feedback", "reference"))
            sub.add_argument("--status", choices=("active", "archived", "all"), default="active")
            sub.add_argument("--cursor")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    for name in ("read", "history", "inspect"):
        sub = commands.add_parser(name)
        sub.add_argument("id")
        sub.add_argument("--json", action="store_true")
        if name == "read":
            sub.add_argument("--revision", type=int)
    reindex = commands.add_parser("reindex")
    reindex.add_argument("--model", type=Path)
    reindex.add_argument("--limit", type=int, default=50)
    export = commands.add_parser("export")
    export.add_argument("target", type=Path)
    for name in ("create", "update", "archive", "restore", "delete", "assess", "approve", "reject"):
        sub = commands.add_parser(name)
        sub.add_argument("--operation-id", required=True)
        if name != "create":
            sub.add_argument("id")
        if name in {"approve", "reject"}:
            sub.add_argument("--expected-proposal-revision", required=True, type=int)
            sub.add_argument("--reviewer", default="cli-user")
            if name == "approve":
                sub.add_argument("--expected-revision", type=int)
                sub.add_argument("--related-records", type=json.loads)
        elif name != "create":
            sub.add_argument("--expected-revision", required=True, type=int)
        if name in {"create", "update"}:
            for field in ("name", "description", "content", "applicability"):
                sub.add_argument(f"--{field}", required=name == "create" and field in {"name", "content"})
            sub.add_argument("--type", choices=("user", "project", "feedback", "reference"))
            sub.add_argument("--source-refs", type=json.loads)
        if name in {"archive", "restore", "delete", "reject"}:
            sub.add_argument("--reason", default="explicit user action")
        if name == "assess":
            for field in ("judgment", "related-records", "resolves-event-ids", "evidence-refs"):
                sub.add_argument(f"--{field}", type=json.loads)
            sub.add_argument("--resolution-reason")
    return parser


def execute_memory(args, memory, *, trusted_context=None) -> dict | list:
    context = dict(trusted_context or {
        "capability": "manage", "scope_id": memory.scope_id, "session_id": "cli-user",
        "actor_id": "cli-user", "operation_namespace": f"{memory.scope_id}:user:cli-user",
    })
    control = MemoryControlPlane(memory, trusted_context=context)
    operation = args.command
    if operation == "migrate":
        from nz_coder.state.memory_migration import migrate_legacy
        return migrate_legacy(memory.memory_dir, memory.scope_id)
    if operation == "reindex":
        from nz_coder.state.memory import configured_embedding_provider, local_embedding_provider
        from nz_coder.state.memory_retrieval import reindex_memory
        provider = local_embedding_provider(args.model) if args.model is not None else configured_embedding_provider()
        return reindex_memory(memory.store, provider, limit=args.limit)
    if operation == "export":
        from nz_coder.state.memory_migration import export_memories
        return export_memories(memory.store, args.target)
    if operation == "list":
        return memory.list_records(type=args.type, status=None if args.status == "all" else args.status,
                                   limit=args.limit, cursor=args.cursor)
    if operation == "search":
        return memory.search_result(args.query, limit=args.limit)
    if operation in {"read", "history", "inspect"}:
        value = (memory.read(args.id, revision=args.revision) if operation == "read" else
                 memory.history(args.id) if operation == "history" else control.get(args.id))
        return value if value is not None else {"status": "not_found", "id": args.id}
    if operation == "pending":
        return control.pending(limit=args.limit)
    if operation == "ledger":
        return control.ledger(limit=args.limit)
    if operation in {"approve", "reject"}:
        kwargs = {"expected_proposal_revision": args.expected_proposal_revision,
                  "operation_id": args.operation_id, "reviewer": args.reviewer}
        if operation == "approve":
            return control.approve(args.id, expected_revision=args.expected_revision,
                                   related_records=args.related_records, **kwargs)
        return control.reject(args.id, reason=args.reason, **kwargs)
    context["operation_id"] = args.operation_id
    return memory.mutate(mutation_command(args), trusted_context=context)


def mutation_command(args) -> dict:
    command = {"action": args.command}
    if args.command != "create":
        command.update(memory_id=args.id, expected_revision=args.expected_revision)
    fields = ("name", "description", "type", "content", "applicability", "source_refs", "reason",
              "judgment", "related_records", "resolves_event_ids", "evidence_refs", "resolution_reason")
    command.update({field: getattr(args, field) for field in fields if getattr(args, field, None) is not None})
    return command


def memory_main(argv: list[str] | None = None, *, manager=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = execute_memory(args, manager or current_memory_manager())
    except (RuntimeError, OSError, sqlite3.Error, ValueError, TypeError) as exc:
        result = {"status": "error", "error": to_public_error(exc).message}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not isinstance(result, dict) or result.get("status", "saved") in {
        "saved", "pending_review", "migrated", "already_imported", "exported", "active", "archived", "pending", "applied", "rejected", "ready"} else 1


__all__ = ["build_parser", "execute_memory", "mutation_command", "memory_main"]
