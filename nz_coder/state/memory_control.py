"""记忆提案的薄门面；保存、审阅和审计均由同一manager事务完成。"""
from __future__ import annotations


class MemoryControlPlane:
    def __init__(self, memory_manager, *, trusted_context: dict | None = None):
        self.manager = memory_manager
        self.context = dict(trusted_context or {
            "capability": "manage", "scope_id": memory_manager.scope_id,
            "session_id": "local-user", "actor_id": "local-user",
            "operation_namespace": f"{memory_manager.scope_id}:user:local-user",
        })

    def _context(self, operation_id, *, reviewer=None, propose=False, source_session=None):
        context = {**self.context, "operation_id": operation_id}
        if reviewer is not None:
            context["actor_id"] = reviewer
        if source_session is not None:
            context["session_id"] = source_session
        if propose and context.get("capability") == "manage":
            context["capability"] = "propose"
        return context

    def submit(self, command: dict, *, operation_id: str, source_session: str | None = None) -> dict:
        return self.manager.mutate(dict(command), trusted_context=self._context(
            operation_id, propose=True, source_session=source_session))

    def pending(self, *, limit=50) -> list[dict]:
        items = []
        for status in ("pending", "pending_review"):
            items.extend(self.manager.proposals(filters={"status": status}, limit=limit,
                                                trusted_context=self.context)["items"])
        return sorted(items, key=lambda item: item["id"])[:limit]

    def get(self, proposal_id: str) -> dict | None:
        return self.manager.read_proposal(proposal_id, trusted_context=self.context)

    def approve(self, proposal_id: str, *, expected_proposal_revision: int,
                operation_id: str, reviewer: str, expected_revision: int | None = None,
                related_records: list | None = None) -> dict:
        command = {"action": "apply_proposal", "proposal_id": proposal_id,
                   "expected_proposal_revision": expected_proposal_revision}
        if expected_revision is not None:
            command["expected_revision"] = expected_revision
        if related_records is not None:
            command["related_records"] = related_records
        return self.manager.mutate(command, trusted_context=self._context(operation_id, reviewer=reviewer))

    def reject(self, proposal_id: str, *, expected_proposal_revision: int,
               operation_id: str, reviewer: str, reason: str) -> dict:
        return self.manager.mutate({"action": "reject_proposal", "proposal_id": proposal_id,
            "expected_proposal_revision": expected_proposal_revision, "reason": reason},
            trusted_context=self._context(operation_id, reviewer=reviewer))

    def ledger(self, *, limit=100) -> list[dict]:
        return self.manager.store.ledger(limit=limit)
