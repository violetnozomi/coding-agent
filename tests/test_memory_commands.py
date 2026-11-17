from __future__ import annotations
import asyncio
from io import StringIO
import json
from rich.console import Console
from nz_coder.interface.commands.registry import CommandContext
from nz_coder.interface.commands.handlers.core import handle_memory, handle_memory_review
from nz_coder.state.memory import MemoryManager
from nz_coder.state.memory_cli import memory_main
from nz_coder.state.memory_control import MemoryControlPlane


def _create(manager, capsys):
    assert memory_main(["create", "--name", "同名", "--type", "project", "--content", "原内容",
                        "--operation-id", "create"], manager=manager) == 0
    return json.loads(capsys.readouterr().out)


def test_cli_memory_versions_receipts_and_lifecycle(tmp_path, capsys):
    manager = MemoryManager(tmp_path / "memory", scope_id="scope")
    created = _create(manager, capsys)
    memory_id = created["memory_id"]
    assert memory_main(["update", memory_id, "--expected-revision", "1", "--content", "新内容",
                        "--operation-id", "update"], manager=manager) == 0
    assert json.loads(capsys.readouterr().out)["revision"] == 2
    assert memory_main(["update", memory_id, "--expected-revision", "1", "--content", "过期",
                        "--operation-id", "stale"], manager=manager) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "conflict"
    for command in (["list"], ["search", "新内容"], ["read", memory_id], ["history", memory_id]):
        assert memory_main(command, manager=manager) == 0
        assert memory_id in capsys.readouterr().out
    for action, revision in (("archive", 2), ("restore", 3), ("delete", 4)):
        assert memory_main([action, memory_id, "--expected-revision", str(revision),
            "--operation-id", action], manager=manager) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "saved"
    assert manager.read(memory_id) is None


def test_cli_review_requires_revision_and_exports_snapshot(tmp_path, capsys):
    manager = MemoryManager(tmp_path / "memory", scope_id="scope")
    control = MemoryControlPlane(manager)
    proposal = control.submit({"action": "create", "name": "候选", "content": "待审核", "type": "project"},
        source_session="session", operation_id="propose")
    proposal_id = proposal["proposal_id"]
    assert memory_main(["pending", "--json"], manager=manager) == 0
    assert proposal_id in capsys.readouterr().out
    assert memory_main(["inspect", proposal_id], manager=manager) == 0
    assert '"revision": 1' in capsys.readouterr().out
    assert memory_main(["approve", proposal_id, "--expected-proposal-revision", "1",
        "--operation-id", "accept"], manager=manager) == 0
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["status"] == "saved"
    assert memory_main(["export", str(tmp_path / "export")], manager=manager) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "exported"
    assert memory_main(["assess", accepted["memory_id"], "--expected-revision", "1", "--operation-id", "assess",
        "--judgment", '{"coverage":"partial","result":"inconclusive","assessment":"只核查一项","covered_claims":"格式"}'],
        manager=manager) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "saved"


class _Controller:
    def __init__(self, manager): self.manager = manager
    def memory_manager(self): return self.manager
    def memory_control(self): return MemoryControlPlane(self.manager)


class _Input:
    interactive = True
    def __init__(self, values, mutate=None):
        self.values = list(values)
        self.calls = []
        self.mutate = mutate
    async def select_async(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 2 and self.mutate:
            self.mutate()
        return self.values.pop(0) if self.values else None
    async def prompt_text_async(self, prompt): return "拒绝"


def _context(manager, args=""):
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None, width=160)
    ctx = CommandContext(history=[], session_state={"id": "terminal", "agent": object(),
        "controller": _Controller(manager)}, system_prompt="", renderer=object(), console=console,
        build_agent=lambda **kw: object(), args=args)
    return ctx, output


def test_terminal_memory_command_uses_same_id_revision_path(tmp_path):
    manager = MemoryManager(tmp_path / "memory", scope_id="scope")
    ctx, output = _context(manager, 'create --name 规则 --content 正文 --operation-id terminal-create')
    handle_memory(ctx)
    memory_id = manager.list_records()["items"][0]["id"]
    ctx.args = f'update {memory_id} --expected-revision 1 --content 新正文 --operation-id terminal-update'
    handle_memory(ctx)
    assert manager.read(memory_id)["content"] == "新正文"
    assert '"status": "saved"' in output.getvalue()


def test_terminal_memory_sql_failure_is_handled_without_exposing_cause(tmp_path, monkeypatch):
    import sqlite3
    manager = MemoryManager(tmp_path / "memory", scope_id="scope")
    marker = "PRIVATE-TERMINAL-SQLITE-CAUSE"
    def fail(**kwargs):
        raise sqlite3.OperationalError(marker)
    monkeypatch.setattr(manager, "list_records", fail)
    ctx, output = _context(manager, "list")
    handle_memory(ctx)
    assert marker not in output.getvalue()
    assert "Memory action failed" in output.getvalue()


def test_terminal_memory_review_sql_failure_does_not_escape(tmp_path, monkeypatch):
    import sqlite3
    manager = MemoryManager(tmp_path / "memory", scope_id="scope")
    marker = "PRIVATE-REVIEW-SQLITE-CAUSE"
    def fail(**kwargs):
        raise sqlite3.OperationalError(marker)
    monkeypatch.setattr(manager, "proposals", fail)
    ctx, output = _context(manager)
    ctx.terminal_input = _Input([])
    asyncio.run(handle_memory_review(ctx))
    assert marker not in output.getvalue()
    assert "Memory review failed" in output.getvalue()


def test_review_selector_uses_displayed_versions_and_reports_stale_decision(tmp_path):
    manager = MemoryManager(tmp_path / "memory", scope_id="scope")
    control = MemoryControlPlane(manager)
    proposal = control.submit({"action": "create", "name": "候选", "content": "正文"}, operation_id="propose")
    proposal_id = proposal["proposal_id"]
    def concurrent_review():
        control.reject(proposal_id, expected_proposal_revision=1, reviewer="other", reason="已处理", operation_id="other")
    ctx, output = _context(manager)
    ctx.terminal_input = _Input([proposal_id, "approve"], concurrent_review)
    asyncio.run(handle_memory_review(ctx))
    assert manager.list_records()["items"] == []
    assert "conflict" in output.getvalue()
    assert "rev 1" in ctx.terminal_input.calls[0]["values"][0][1]


def test_cli_reindex_without_ready_model_does_not_encode(tmp_path, capsys, monkeypatch):
    from nz_coder.state import memory as memory_module
    monkeypatch.setattr(memory_module, "configured_embedding_provider", lambda: None)
    manager = MemoryManager(tmp_path / "memory", scope_id="scope")
    assert memory_main(["reindex"], manager=manager) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_remote_memory_review_and_update_keep_explicit_versions():
    from nz_coder.interface.remote import _memory_command
    class Backend:
        def __init__(self): self.calls = []
        def review_memory(self, proposal_id, action, **kwargs):
            self.calls.append((proposal_id, action, kwargs))
            return {"status": "conflict", "proposal_id": proposal_id, "proposal_revision": 2}
        def mutate_memory(self, command, **kwargs):
            self.calls.append((command, kwargs))
            return {"status": "saved", "memory_id": "memory-id", "revision": 4}
    backend = Backend()
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)
    asyncio.run(_memory_command(backend, console,
        "/memory approve proposal-id --expected-proposal-revision 1 --expected-revision 3 --operation-id review"))
    assert backend.calls[0] == ("proposal-id", "approve", {"expected_proposal_revision": 1,
        "expected_revision": 3, "related_records": None, "operation_id": "review", "reason": ""})
    assert "conflict" in output.getvalue()
    asyncio.run(_memory_command(backend, console,
        "/memory update memory-id --expected-revision 3 --content 正文 --operation-id update"))
    assert backend.calls[1] == ({"action": "update", "memory_id": "memory-id", "expected_revision": 3,
        "content": "正文"}, {"operation_id": "update"})


def test_remote_memory_bad_quotation_keeps_next_command_available():
    from nz_coder.interface.remote import _memory_command

    class Backend:
        def __init__(self):
            self.queries = []
            self.closed = False

        def search_memories(self, query, **options):
            self.queries.append(query)
            return {"items": [], "semantic_status": "lexical", "generation": [0, 0]}

        def close(self):
            self.closed = True

    backend = Backend()
    output = StringIO()
    console = Console(file=output, force_terminal=False, color_system=None)

    async def scenario():
        await _memory_command(backend, console, '/memory search "unfinished')
        await _memory_command(backend, console, '/memory search "编译流程"')

    asyncio.run(scenario())
    assert backend.closed is False
    assert backend.queries == ["编译流程"]
    assert "check quotation marks and escape characters" in output.getvalue()
    assert '"semantic_status": "lexical"' in output.getvalue()


def test_cli_does_not_expose_private_backend_exceptions(tmp_path, capsys, monkeypatch):
    import sqlite3
    from nz_coder.protocol.public_error import to_public_error
    manager = MemoryManager(tmp_path / "memory", scope_id="scope")
    marker = "PRIVATE_CLI_ERROR_42581"
    for failure in (OSError, sqlite3.OperationalError):
        def fail_list(**kwargs):
            raise failure(marker)
        monkeypatch.setattr(manager, "list_records", fail_list)
        assert memory_main(["list"], manager=manager) == 1
        output = capsys.readouterr().out
        assert marker not in output
        assert json.loads(output)["error"] == to_public_error(failure(marker)).message
