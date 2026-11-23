"""为记忆提案附上提交人或审阅人的身份，再交给记忆管理器处理。"""
from __future__ import annotations


class MemoryControlPlane:
    def __init__(self, memory_manager, *, trusted_context: dict | None = None):
        self.manager = memory_manager
        self.context = dict(trusted_context or {
            "capability": "manage",
            "scope_id": memory_manager.scope_id,
            "session_id": "local-user",
            "actor_id": "local-user",
            "operation_namespace": f"{memory_manager.scope_id}:user:local-user",
        })

    def _operation_context(self, operation_id, *, reviewer=None, propose=False, source_session=None):
        context = {**self.context, "operation_id": operation_id}
        if reviewer is not None:
            context["actor_id"] = reviewer
        if source_session is not None:
            context["session_id"] = source_session
        # 管理员提交的提案也要等待审阅。只读调用方保留原权限。
        if propose and context.get("capability") == "manage":
            context["capability"] = "propose"
        return context

    def submit(self, command: dict, *, operation_id: str, source_session: str | None = None) -> dict:
        context = self._operation_context(operation_id, propose=True, source_session=source_session)
        return self.manager.mutate(dict(command), trusted_context=context)

    def pending(self, *, limit=50) -> list[dict]:
        items = []
        # 本地新提案和导入的旧提案有两种待审状态，合并后再截取展示数量。
        for status in ("pending", "pending_review"):
            page = self.manager.proposals(filters={"status": status}, limit=limit, trusted_context=self.context)
            items.extend(page["items"])
        return sorted(items, key=lambda item: item["id"])[:limit]

    def get(self, proposal_id: str) -> dict | None:
        return self.manager.read_proposal(proposal_id, trusted_context=self.context)

    def approve(
        self,
        proposal_id: str,
        *,
        expected_proposal_revision: int,
        operation_id: str,
        reviewer: str,
        expected_revision: int | None = None,
        related_records: list | None = None,
    ) -> dict:
        """传入审阅时的提案版本。提案涉及已有记忆时，还要传入审阅者看到的记忆版本。"""
        command = {
            "action": "apply_proposal",
            "proposal_id": proposal_id,
            "expected_proposal_revision": expected_proposal_revision,
        }
        if expected_revision is not None:
            command["expected_revision"] = expected_revision
        if related_records is not None:
            command["related_records"] = related_records
        context = self._operation_context(operation_id, reviewer=reviewer)
        return self.manager.mutate(command, trusted_context=context)

    def reject(
        self,
        proposal_id: str,
        *,
        expected_proposal_revision: int,
        operation_id: str,
        reviewer: str,
        reason: str,
    ) -> dict:
        command = {
            "action": "reject_proposal",
            "proposal_id": proposal_id,
            "expected_proposal_revision": expected_proposal_revision,
            "reason": reason,
        }
        context = self._operation_context(operation_id, reviewer=reviewer)
        return self.manager.mutate(command, trusted_context=context)

    def ledger(self, *, limit=100) -> list[dict]:
        return self.manager.store.ledger(limit=limit)
