"""Non-interactive memory review controls for automation and operators."""
from __future__ import annotations

import argparse
import json
import sys

from nz_coder.state.memory import current_memory_manager
from nz_coder.state.memory_control import MemoryControlPlane


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nz-coder memory")
    commands = parser.add_subparsers(dest="command", required=True)
    pending = commands.add_parser("pending", help="List proposals waiting for review")
    pending.add_argument("--json", action="store_true")
    inspect = commands.add_parser("inspect", help="Inspect one proposal")
    inspect.add_argument("fingerprint")
    inspect.add_argument("--json", action="store_true")
    approve = commands.add_parser("approve", help="Approve one proposal")
    approve.add_argument("fingerprint")
    approve.add_argument("--reviewer", default="cli-user")
    reject = commands.add_parser("reject", help="Reject one proposal")
    reject.add_argument("fingerprint")
    reject.add_argument("--reason", default="rejected by CLI user")
    reject.add_argument("--reviewer", default="cli-user")
    ledger = commands.add_parser("ledger", help="Show the append-only review ledger")
    ledger.add_argument("--json", action="store_true")
    edit = commands.add_parser("edit", help="Curate an existing persisted memory")
    edit.add_argument("name")
    edit.add_argument("--description")
    edit.add_argument("--type", dest="memory_type")
    edit.add_argument("--content")
    delete = commands.add_parser("delete", help="Delete one persisted memory")
    delete.add_argument("name")
    delete.add_argument("--confirm", action="store_true")
    return parser


def memory_main(argv: list[str] | None = None, *, manager=None) -> int:
    args = build_parser().parse_args(argv)
    memory = manager or current_memory_manager()
    control = MemoryControlPlane(memory.memory_dir, memory)
    if args.command in {"edit", "delete"}:
        if not memory.memories:
            memory.load_all()
        existing = memory.memories.get(args.name)
        if existing is None:
            print(f"Error: memory not found: {args.name}")
            return 1
        if args.command == "delete":
            if not args.confirm:
                print("Error: --confirm is required to delete a memory", file=sys.stderr)
                return 2
            result = memory.delete(args.name)
        else:
            result = memory.save(
                args.name,
                args.description if args.description is not None else str(existing.get("description") or ""),
                args.memory_type if args.memory_type is not None else str(existing.get("type") or "project"),
                args.content if args.content is not None else str(existing.get("content") or ""),
            )
        print(result)
        return 1 if str(result).startswith("Error: ") else 0
    if args.command == "pending":
        proposals = control.pending()
        if args.json:
            print(json.dumps([_proposal_dict(item) for item in proposals], ensure_ascii=False, indent=2))
        elif not proposals:
            print("No memory proposals are pending review.")
        else:
            for item in proposals:
                print(
                    f"{item.fingerprint}  {item.risk}  {item.confidence:.2f}  "
                    f"{item.source_session or '-'}  {item.name}"
                )
        return 0
    if args.command == "ledger":
        events = control.ledger()
        if args.json:
            print(json.dumps(events, ensure_ascii=False, indent=2))
        else:
            for item in events[-100:]:
                print(
                    f"{item.get('action', 'unknown'):16} "
                    f"{str(item.get('fingerprint') or '')[:16]} "
                    f"{item.get('status', '')}"
                )
        return 0
    if args.command == "inspect":
        proposal = control.get(args.fingerprint)
        if proposal is None:
            print(f"Error: Unknown memory proposal '{args.fingerprint}'")
            return 1
        if args.json:
            print(json.dumps(_proposal_dict(proposal), ensure_ascii=False, indent=2))
        else:
            print(_format_proposal(proposal))
        return 0
    try:
        if args.command == "approve":
            proposal = control.approve(args.fingerprint, reviewer=args.reviewer)
        else:
            proposal = control.reject(
                args.fingerprint,
                reviewer=args.reviewer,
                reason=args.reason,
            )
    except (KeyError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    print(f"Memory proposal {proposal.fingerprint} is {proposal.status}.")
    return 0 if proposal.status in {"applied", "rejected"} else 1


def _proposal_dict(proposal) -> dict:
    return {
        "source_session": proposal.source_session,
        "source_message_ids": list(proposal.source_message_ids),
        "name": proposal.name,
        "description": proposal.description,
        "type": proposal.type,
        "content": proposal.content,
        "confidence": proposal.confidence,
        "reason": proposal.reason,
        "created_at": proposal.created_at,
        "fingerprint": proposal.fingerprint,
        "risk": proposal.risk,
        "status": proposal.status,
    }


def _format_proposal(proposal) -> str:
    return (
        f"Name: {proposal.name}\n"
        f"Description: {proposal.description}\n"
        f"Type: {proposal.type}\n"
        f"Risk: {proposal.risk}\n"
        f"Confidence: {proposal.confidence:.2f}\n"
        f"Status: {proposal.status}\n"
        f"Source session: {proposal.source_session or '-'}\n"
        f"Reason: {proposal.reason}\n\n"
        f"{proposal.content}\n\n"
        f"Fingerprint: {proposal.fingerprint}"
    )


__all__ = ["build_parser", "memory_main"]
