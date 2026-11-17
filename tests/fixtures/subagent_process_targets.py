"""Importable offline spawn targets for child supervision behavior tests."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
import time

from nz_coder.runtime.agent.child_process_result import ChildProcessResultFile


def _write_large_result(result_path, payload):
    from nz_coder.runtime.agent.child_process_result import write_child_process_result

    write_child_process_result(result_path, {
        "schema": "nz.child_result.v1", "ok": True,
        "result": "中" * payload.get("characters", 512 * 1024),
    })


def _block_body(result_path):
    (result_path.path.parent / "ready").write_text("ready")
    time.sleep(30)


def large_result(result_path: ChildProcessResultFile, payload: dict, endpoint) -> None:
    with _command_client(payload, endpoint):
        _write_large_result(result_path, payload)


def blocking(result_path: ChildProcessResultFile, payload: dict, endpoint) -> None:
    with _command_client(payload, endpoint):
        _block_body(result_path)


def result_then_wait(result_path: ChildProcessResultFile, payload: dict, endpoint) -> None:
    with _command_client(payload, endpoint):
        _write_large_result(result_path, payload)
        _block_body(result_path)


def exit_without_result(result_path: ChildProcessResultFile, payload: dict, endpoint) -> None:
    with _command_client(payload, endpoint):
        pass
    os._exit(payload.get("exit_code", 0))


def invalid_result(result_path: ChildProcessResultFile, payload: dict, endpoint) -> None:
    with _command_client(payload, endpoint):
        result_path.path.write_text("sentinel-secret-invalid-json")
        result_path.path.chmod(0o600)


def failed_write(result_path: ChildProcessResultFile, payload: dict, endpoint) -> None:
    with _command_client(payload, endpoint):
        result_path.path.mkdir()
        _write_large_result(result_path, payload)


def _managed_body(payload, emit, transport_path, before_crash=None):
    from nz_coder.runtime.agent.subagent import _load_subagent_state, _save_subagent_state

    workspace = Path(payload["workspace"])
    session_id = payload["session_id"]
    (workspace / f"{session_id}-{time.time_ns()}.transport").write_text(str(transport_path))
    if payload["prompt"] == "block":
        (workspace / f"{session_id}.ready").write_text("ready")
        time.sleep(30)
        return
    state = _load_subagent_state(payload["parent_session_id"], session_id, workspace)
    if payload["prompt"] == "early-error":
        from nz_coder.protocol.public_error import PublicError

        (workspace / f"{session_id}.prior-cost").write_text(str(state.get("child_cost_baseline")))
        emit({"schema": "nz.child_result.v1", "ok": False,
              "public_error": PublicError("child_runtime_error", "Current invocation failed before completion.").to_dict()})
        return
    state.update({
        "status": "completed", "background_result": "parent has not accepted this yet",
        "_session_authoritative": True,
        "child_result": {
            "task_id": session_id, "session_id": session_id, "parent_session_id": payload["parent_session_id"], "name": "offline",
            "status": "completed", "final_text": "done",
            "digest": "successful summary", "summary_kind": "digest", "final_text_truncated": True,
            "usage": {"input": 11, "output": 7, "total": 18}, "cost": 0.125,
            "structured": {"answer": 42},
        },
    })
    if state.get("settlement_id"):
        state["child_result_settlement_id"] = state["settlement_id"]
    _save_subagent_state(payload["parent_session_id"], state, workspace)
    emit({"schema": "nz.child_result.v1", "ok": True, "result": "done"})
    (workspace / f"{session_id}.ready").write_text("ready")
    if payload["prompt"] == "complete-and-crash":
        if before_crash is not None:
            before_crash()
        os._exit(17)
    if payload["prompt"] == "complete-with-invalid-transport":
        Path(transport_path).write_text("sentinel-secret-invalid-json")
    if payload["prompt"] == "complete-and-block":
        time.sleep(30)


def managed_child(result_path: ChildProcessResultFile, payload: dict, endpoint) -> None:
    from nz_coder.runtime.agent.child_process_result import write_child_process_result

    with _command_client(payload, endpoint) as client:
        _managed_body(payload, lambda envelope: write_child_process_result(result_path, envelope), result_path.path,
                      before_crash=lambda: client.finish(deadline=payload['command_deadline']))


def owned_process_then_complete(result_path, payload, endpoint):
    from nz_coder.runtime.agent.child_process_result import write_child_process_result

    with _command_client(payload, endpoint) as client:
        started = client.call('process', {'operation': 'start', 'command': 'controlled', 'tty': False}, cancel_event=None)
        assert not started.startswith('Error:'), started
        _managed_body(payload, lambda envelope: write_child_process_result(result_path, envelope), result_path.path)


def parent_owned_probe(result_path, payload, endpoint):
    import json
    from nz_coder.runtime.agent.child_process_result import write_child_process_result

    workspace = Path(payload['workspace'])
    with _command_client(payload, endpoint) as client:
        if payload['command_tool'] == 'bash':
            output = client.call('bash', {'command': payload['command']}, cancel_event=None)
            (workspace / 'command-output.json').write_text(json.dumps(str(output)))
            write_child_process_result(result_path, {'schema': 'nz.child_result.v1', 'ok': True, 'result': str(output)})
            return
        started = json.loads(client.call('process', {'operation': 'start', 'command': payload['command'], 'tty': payload['tty']}, cancel_event=None))
        process_id = started['process']['process_id']
        facts = {'started': started, 'reads': []}
        if payload['tty']:
            facts['resize'] = json.loads(client.call('process', {'operation': 'resize', 'process_id': process_id, 'rows': 31, 'cols': 99}, cancel_event=None))
        facts['write'] = json.loads(client.call('process', {'operation': 'write', 'process_id': process_id, 'data': '你好\n'}, cancel_event=None))
        cursor = 0
        text = ''
        while '尾部' not in text:
            reply = json.loads(client.call('process', {'operation': 'read', 'process_id': process_id, 'cursor': cursor, 'wait_seconds': .2}, cancel_event=None))
            facts['reads'].append(reply)
            cursor = reply['next_cursor']
            text += reply['output']
        facts['tail'] = json.loads(client.call('process', {'operation': 'read', 'process_id': process_id, 'tail_bytes': 512}, cancel_event=None))
        (workspace / 'process-probe.json').write_text(json.dumps(facts))
        time.sleep(30)


def canonical_result(result_path: ChildProcessResultFile, payload: dict, endpoint) -> None:
    """Exercise the production child entry with offline finalization/save failure."""
    from unittest.mock import patch
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.process_supervisor import _run_subagent_process

    state = {
        "session_id": payload["session_id"], "parent_session_id": payload["parent_session_id"],
        "workspace_root": payload["workspace"], "settlement_id": payload["settlement_id"],
        "agent_type": "explore", "status": "completed", "tokens": {"total": 18},
        "cost_known": True, "cost": 0.125, "structured_output": {"answer": 42},
    }
    with patch.object(subagent, "_save_subagent_state", side_effect=OSError("injected child save failure")):
        output = subagent._finalize_subagent_result("done", Path(payload["workspace"]) / "missing.md", "completed", state)
    wrong = payload.get("wrong_identity")
    if wrong == "settlement":
        output.metadata["child_result_settlement_id"] = "old-round"
    elif wrong:
        output.metadata["child_result"][wrong] = "other-task"
    def run(*args, **kwargs):
        from nz_coder.runtime.process.command_channel import current_command_client

        client = current_command_client()
        assert client.policy_ready(_fixture_policy(payload), deadline=payload['command_deadline'])
        return output
    with patch.object(subagent, "run_subagent", run):
        _run_subagent_process(result_path, payload, endpoint)
    if payload.get("block_after_result"):
        _block_body(result_path)


def _fixture_policy(payload):
    from nz_coder.runtime.agent.subagent import _load_subagent_state, _preview_subagent_command_policy
    from nz_coder.runtime.process.workdir import scoped_workdir

    root = Path(payload['workspace'])
    with scoped_workdir(root):
        state = _load_subagent_state(payload['parent_session_id'], payload['session_id'], root)
    prepared = payload['prepared_workspace']
    return _preview_subagent_command_policy(
        payload['prompt'], state, execution_root=prepared.execution_root,
        config_snapshot=prepared.config_snapshot, settings=payload['command_settings'],
    )


@contextmanager
def _command_client(payload, endpoint):
    from nz_coder.runtime.process.command_channel import ChildCommandClient

    client = ChildCommandClient(endpoint, limits=payload['command_limits'], deadline=payload['command_deadline'])
    client.set_fact_receiver(lambda _fact: None)
    assert client.policy_ready(_fixture_policy(payload), deadline=payload['command_deadline'])
    try:
        yield client
    finally:
        client.finish(deadline=payload['command_deadline'])
        while not client.close(deadline=time.monotonic() + .05):
            pass


def _write_canonical_result(result_path, payload):
    from nz_coder.runtime.agent.child_process_result import write_child_process_result

    write_child_process_result(result_path, {
        'schema': 'nz.child_result.v1', 'ok': True, 'result': 'done',
        'child_result_settlement_id': payload['settlement_id'],
        'child_result': {
            'task_id': payload['session_id'], 'session_id': payload['session_id'],
            'parent_session_id': payload['parent_session_id'], 'name': 'offline',
            'status': 'completed', 'final_text': 'done', 'usage': {'input': 11, 'output': 7, 'total': 18}, 'cost': .125,
        },
    })


def finish_then_result(result_path, payload, endpoint):
    with _command_client(payload, endpoint) as client:
        assert client.finish(deadline=payload['command_deadline'])
        assert client.close(deadline=payload['command_deadline'])
        root = Path(payload['workspace'])
        (root / 'finished-channel').touch()
        while not (root / 'release-result').exists():
            time.sleep(.01)
        _write_canonical_result(result_path, payload)


def canonical_then_broken_channel(result_path, payload, endpoint):
    with _command_client(payload, endpoint) as client:
        _write_canonical_result(result_path, payload)
        assert client.close(deadline=payload['command_deadline'])
        time.sleep(30)


def drop_command_channel(result_path, payload, endpoint):
    if payload['mode'] == 'protocol':
        endpoint.socket.sendall(b'\x00\x00\x00\x02{}')
        time.sleep(30)
        return
    with _command_client(payload, endpoint) as client:
        assert client.close(deadline=payload['command_deadline'])
        time.sleep(30)


def production_probe(result_path, payload, endpoint):
    """Use the production child/lifecycle with an offline SDK response boundary."""
    import json
    from types import SimpleNamespace
    from unittest.mock import patch
    from openai.types.chat import ChatCompletion
    from nz_coder.runtime.agent import subagent
    from nz_coder.runtime.agent.process_supervisor import _run_subagent_process
    from nz_coder.runtime.core.execution_context import strict_local_tools
    from nz_coder.runtime.process.workdir import current_workdir

    import threading
    from nz_coder.runtime.execution.run_lifecycle import ProductionRunLifecycle
    from nz_coder.runtime.process.command_channel import ChildCommandClient
    from nz_coder.tools import ToolOutput

    facts = {'model_calls': 0, 'tool_outputs': [], 'tool_replies': []}
    lock = threading.Lock()
    def save():
        (Path(payload['workspace']) / '.nz-coder').mkdir(exist_ok=True)
        (Path(payload['workspace']) / '.nz-coder' / 'model-probe.json').write_text(json.dumps(facts))
    original_initialize = ProductionRunLifecycle.initialize
    def initialize(self, context, messages, stream):
        result = original_initialize(self, context, messages, stream)
        with lock:
            facts.update(cwd=str(current_workdir()), initial_strict=strict_local_tools(), restored=context.run_state.restored_state)
            save()
        return result
    original_call = ChildCommandClient.call
    def call(self, tool, arguments, *, cancel_event):
        with lock:
            facts['strict_after_tools'] = strict_local_tools()
            save()
        result = original_call(self, tool, arguments, cancel_event=cancel_event)
        with lock:
            facts['tool_replies'].append({'tool': tool, 'text': str(result), 'title': result.title if isinstance(result, ToolOutput) else '', 'metadata': result.metadata if isinstance(result, ToolOutput) else {}})
            save()
        return result
    def create(**kwargs):
        facts['model_calls'] += 1
        facts['system'] = [m.get('content', '') for m in kwargs['messages'] if m.get('role') == 'system']
        facts['tool_outputs'] = [message['content'] for message in kwargs['messages'] if message.get('role') == 'tool']
        (Path(payload['workspace']) / '.nz-coder').mkdir(exist_ok=True)
        (Path(payload['workspace']) / '.nz-coder' / 'model-probe.json').write_text(json.dumps(facts))
        if payload['probe_mode'] == 'retrieval' and facts['model_calls'] == 1 and payload['prompt'] != 'retrieval:evidence':
            tool = payload['prompt'].split(':', 1)[1]
            arguments = {} if tool == 'diff_status' else {'pattern': '*.py' if tool == 'glob_search' else 'needle'}
            message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'retrieval-1', 'type': 'function', 'function': {'name': tool, 'arguments': json.dumps(arguments)}}]}
            finish = 'tool_calls'
        elif (payload['probe_mode'] == 'restore' and facts['model_calls'] <= 3) or (payload['probe_mode'] == 'worktree' and facts['model_calls'] == 1):
            command = ('pwd' if payload['probe_mode'] == 'worktree' else 'cat tests/test_sample.py'
                       if facts['model_calls'] == 2 else 'python -m unittest discover -s tests')
            message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': f'probe-{facts["model_calls"]}', 'type': 'function', 'function': {'name': 'bash', 'arguments': json.dumps({'command': command})}}]}
            finish = 'tool_calls'
        else:
            message, finish = {'role': 'assistant', 'content': 'Offline probe finished.'}, 'stop'
            if payload['probe_mode'] == 'verification':
                message['content'] = 'short' if facts['model_calls'] == 1 else 'A sufficiently detailed terminal report.'
        return ChatCompletion(id='offline', created=0, model='offline', object='chat.completion', choices=[{'index': 0, 'finish_reason': finish, 'message': message}])
    import socket
    original_connect = socket.socket.connect
    def connect(stream, address):
        if stream.family in {socket.AF_INET, socket.AF_INET6}:
            raise AssertionError('Provider network is disabled in this offline child')
        return original_connect(stream, address)
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    with (
        patch.object(socket.socket, 'connect', connect),
        patch.object(subagent, 'OpenAI', lambda **kwargs: client),
        patch.object(ProductionRunLifecycle, 'initialize', initialize),
        patch.object(ChildCommandClient, 'call', call),
        patch.object(subagent, '_ensure_subagent_worktree', side_effect=AssertionError('child re-created prepared worktree')),
    ):
        _run_subagent_process(result_path, payload, endpoint)


def prepared_worktree_probe(result_path, payload, endpoint):
    """在真实manager payload上使用同一个离线生产链路探针。"""
    production_probe(result_path, {**payload, "probe_mode": "worktree"}, endpoint)


def invalid_evidence_probe(result_path, payload, endpoint):
    from unittest.mock import patch
    from nz_coder.runtime.agent import process_supervisor, subagent, child_contracts
    def provider(**kwargs):
        (Path(payload['workspace'])/'forbidden-provider').touch()
        raise AssertionError('invalid snapshot reached Provider')
    def git(*args, **kwargs):
        (Path(payload['workspace'])/'forbidden-child-git').touch()
        raise AssertionError('invalid snapshot reached child Git')
    with patch.object(subagent,'OpenAI',provider), patch.object(child_contracts.subprocess,'run',git):
        process_supervisor._run_subagent_process(result_path,payload,endpoint)


def retrieval_probe(result_path, payload, endpoint):
    from unittest.mock import patch
    from nz_coder.runtime.agent import child_contracts
    if payload["prompt"] == "retrieval:evidence":
        original_run = child_contracts.subprocess.run
        def run(argv, *args, **kwargs):
            if list(argv[:3]) == ["git", "diff", "--no-ext-diff"]:
                raise AssertionError("prepared evidence reached child Git")
            return original_run(argv, *args, **kwargs)
        with patch.object(child_contracts.subprocess, "run", run):
            production_probe(result_path, {**payload, "probe_mode": "retrieval"}, endpoint)
    else:
        production_probe(result_path, {**payload, "probe_mode": "retrieval"}, endpoint)


def retrieval_canonical_probe(result_path, payload, endpoint):
    import json
    with _command_client(payload, endpoint) as client:
        tool = payload["prompt"].split(":", 1)[1]
        arguments = {} if tool == "diff_status" else {"pattern": "*.py" if tool == "glob_search" else "needle"}
        output = client.call(tool, arguments, cancel_event=None)
        (Path(payload["workspace"])/"canonical-tool.json").write_text(json.dumps(str(output)))
        assert not output.startswith("Error:"), output
        _write_canonical_result(result_path, payload)
