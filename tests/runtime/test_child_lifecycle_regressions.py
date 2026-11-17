"""Exercise real child entrypoints and durable fork ownership without network."""
import asyncio
import copy
import threading
from pathlib import Path

import pytest

from nz_coder.runtime.agent import subagent
from nz_coder.runtime.agent.agent_manager import BackgroundAgentManager, _LiveJob, scoped_background_agent_manager
from nz_coder.runtime.agent.settlement import freeze_settlement
from nz_coder.runtime.process.workdir import scoped_workdir, current_workdir
from nz_coder.runtime.session.model import Session, SessionIdentity
from nz_coder.runtime.session.store import LegacyJsonSessionStore
from nz_coder.runtime.workflows.workflow_runtime import WorkflowRuntime, _SUCCESS


@pytest.mark.parametrize('mode', ['parallel', 'pipeline', 'map_reduce'])
def test_workflow_real_children_keep_parent_context(tmp_path, monkeypatch, mode):
    from tests.test_subagent import FakeClient, FakeResponse, FakeMessage
    from nz_coder.foundation import config
    observed = []
    def client(**kwargs):
        observed.append((subagent._parent_session_id(), current_workdir()))
        return FakeClient([FakeResponse(FakeMessage('offline evidence'))])
    monkeypatch.setattr(subagent, 'OpenAI', client)
    monkeypatch.setattr(config, 'SUBAGENT_MAX_TURNS', 2)
    manager = BackgroundAgentManager(tmp_path, 'workflow-parent')
    task = {'prompt': 'inspect {item}', 'read_only': True}
    phase = {'name': 'inspect', 'mode': mode}
    if mode == 'parallel':
        phase['tasks'] = [dict(task), dict(task)]
    elif mode == 'pipeline':
        phase.update(items=['a', 'b'], stages=[task])
    else:
        phase.update(items=['a', 'b'], map=task, rubric='summarize evidence')
    try:
        with scoped_workdir(tmp_path), subagent.scoped_parent_context(session_id='workflow-parent'), scoped_background_agent_manager(manager):
            result = WorkflowRuntime(manager, run_id='real-' + mode).execute({'phases': [phase]})
        assert observed, result
        assert all(parent == 'workflow-parent' for parent, _ in observed)
        assert all(path == tmp_path for _, path in observed)
        assert all(s['status'] in _SUCCESS for s in manager._states())
        assert all(s['parent_session_id'] == 'workflow-parent' for s in manager._states())
    finally:
        manager.close()


@pytest.mark.parametrize('status', ['needs_parent', 'completed_conflicted'])
def test_durable_followup_status_is_waitable_not_success(tmp_path, status):
    manager = BackgroundAgentManager(tmp_path, 'parent')
    try:
        state = dict(session_id=status, parent_session_id='parent', background=True, status='queued', settlement_id=status + '-one')
        manager._save(state)
        manager._workflow.record_task('task_queued', state)
        state['status'] = status
        frozen = freeze_settlement(state, 'follow-up evidence', settlement_id=state['settlement_id'])
        manager._save(frozen)
        manager._workflow.commit_terminal(frozen, event_id=status + '-terminal')
        done = threading.Event()
        done.set()
        manager._jobs[status] = _LiveJob(threading.Event(), done, threading.Thread(), settlement_id=state['settlement_id'], frozen=True)
        assert manager.wait([status], timeout_ms=1).metadata['unsettled_task_ids'] == []
        assert manager.wait_for_result(status).status == status
        assert status not in _SUCCESS
    finally:
        manager.close()


@pytest.mark.parametrize('sender', ['worker', 'thread-child', 'other-process'])
@pytest.mark.parametrize('recipient', ['process-child', '*'])
def test_process_mailbox_rejects_all_senders_atomically(tmp_path, sender, recipient):
    manager = BackgroundAgentManager(tmp_path, 'parent')
    try:
        for child in ['thread-child', 'process-child', 'other-process']:
            manager._save(dict(session_id=child, parent_session_id='parent', background=True, status='running', isolation='thread' if child == 'thread-child' else 'process'))
        result = manager.send_message(sender=sender, recipient=recipient, content='new requirement')
        assert 'unavailable' in result
        assert not manager._mailboxes
    finally:
        manager.close()


def _reference(child):
    return {'role': 'assistant', 'content': '', '_nz_parts': [{'type': 'tool', 'tool': 'task', 'call_id': 'task-call', 'state': {'status': 'completed', 'input': {}, 'output': '', 'metadata': {'child_session_id': child}}}]}


def _native_source(workspace):
    from nz_coder.runtime.worktree import WorktreeManager
    with scoped_workdir(workspace):
        source = subagent._new_subagent_state('old-parent', 'general-purpose', None)
    source['status'] = 'completed'
    tree = WorktreeManager(workspace).worktree_dir / source['session_id']
    tree.mkdir(parents=True)
    (tree / 'note.txt').write_text('original')
    source['worktree'] = dict(id=source['session_id'], path=str(tree), branch='', based_on='HEAD', head_commit='', mode='copy')
    session = Session.create(source['session_id'], [{'role': 'user', 'content': 'remember this'}, {'role': 'assistant', 'content': 'original findings'}], workspace=tree, parent_session_id='old-parent')
    asyncio.run(LegacyJsonSessionStore().save(session))
    subagent._save_subagent_state('old-parent', source, workspace)
    return source, tree


def test_native_fork_history_and_display_use_child_workspaces_in_running_loop(tmp_path):
    source, tree = _native_source(tmp_path)
    messages = [_reference(source['session_id'])]
    async def fork_and_display():
        mapping = subagent.clone_referenced_subagents('old-parent', 'new-parent', messages, workspace_root=tmp_path)
        target_id = mapping.mapping[source['session_id']]
        display = subagent.load_subagent_session('new-parent', target_id, tmp_path)
        listing = subagent.list_subagent_sessions('new-parent', tmp_path)
        return mapping, display, listing
    mapping, display, listing = asyncio.run(fork_and_display())
    target_id = mapping.mapping[source['session_id']]
    target = subagent._load_subagent_state('new-parent', target_id, tmp_path)
    native = asyncio.run(LegacyJsonSessionStore().load(SessionIdentity(target_id, 'new-parent'), Path(target['worktree']['path'])))
    assert native is not None
    assert [m['content'] for m in native.transcript] == ['remember this', 'original findings']
    assert {m['_nz_session_id'] for m in native.transcript} == {target_id}
    assert display['messages'] == native.transcript
    assert listing[0]['message_count'] == 2
    assert 'messages' not in target
    assert (tree / 'note.txt').read_text() == 'original'


def _snapshot_source(workspace):
    from tests.test_session_revert import _history
    from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
    from nz_coder.state.sessions import session_snapshot_dir
    source, tree = _native_source(workspace)
    with scoped_workdir(tree):
        snapshots = WorkspaceSnapshotStore(tree, session_snapshot_dir(source['session_id']))
    _, _, history = _history(tree, store=snapshots, session_id=source['session_id'])
    session = Session.create(source['session_id'], history, workspace=tree, parent_session_id='old-parent')
    asyncio.run(LegacyJsonSessionStore().save(session))
    return source, tree, snapshots


def test_native_fork_copies_snapshots_and_cleanup_is_owned_and_repeatable(tmp_path):
    from nz_coder.runtime.process.workspace_snapshot import WorkspaceSnapshotStore
    from nz_coder.runtime.session.session_revert import SessionReverter
    from nz_coder.state.sessions import session_snapshot_dir, _session_path
    source, tree, snapshots = _snapshot_source(tmp_path)
    messages = [_reference(source['session_id'])]
    result = subagent.clone_referenced_subagents('old-parent', 'new-parent', messages, workspace_root=tmp_path)
    target_id = result.mapping[source['session_id']]
    target = subagent._load_subagent_state('new-parent', target_id, tmp_path)
    target_tree = Path(target['worktree']['path'])
    with scoped_workdir(target_tree):
        root = session_snapshot_dir(target_id)
        native_path = _session_path(target_id)
    native = asyncio.run(LegacyJsonSessionStore().load(SessionIdentity(target_id, 'new-parent'), target_tree))
    reverter = SessionReverter(WorkspaceSnapshotStore(target_tree, root), root / 'revert.json')
    reverter.revert(native.transcript)
    assert (target_tree / 'app.py').read_text() == 'before\n'
    assert (tree / 'app.py').read_text() == 'after\n'
    subagent.discard_cloned_subagents(result)
    subagent.discard_cloned_subagents(result)
    assert not target_tree.exists()
    assert not native_path.exists()
    assert not root.exists()
    assert tree.exists() and snapshots.root.exists()
    assert subagent._load_subagent_state('old-parent', source['session_id'], tmp_path)


def test_native_fork_partial_save_failure_cleans_only_new_artifacts(tmp_path, monkeypatch):
    from nz_coder.state.sessions import _session_path, session_snapshot_dir
    source, tree, snapshots = _snapshot_source(tmp_path)
    messages = [_reference(source['session_id'])]
    before = copy.deepcopy(messages)
    created = []
    original = LegacyJsonSessionStore.save_session
    def fail_after_save(self, session):
        original(self, session)
        with scoped_workdir(session.workspace):
            created.append((session.workspace, _session_path(session.session_id), session_snapshot_dir(session.session_id)))
        raise OSError('fork save fault')
    monkeypatch.setattr(LegacyJsonSessionStore, 'save_session', fail_after_save)
    with pytest.raises(OSError, match='fork save fault'):
        subagent.clone_referenced_subagents('old-parent', 'new-parent', messages, workspace_root=tmp_path)
    assert messages == before
    assert created and all(not path.exists() for group in created for path in group)
    assert snapshots.root.exists() and (tree / 'app.py').read_text() == 'after\n'
    assert not subagent.list_subagent_sessions('new-parent', tmp_path)


def test_native_nested_fork_rebinds_parent_and_result_references(tmp_path):
    from nz_coder.runtime.agent.child_result import ChildAgentResult
    source, tree = _native_source(tmp_path)
    nested = subagent._new_subagent_state(source['session_id'], 'explore', None)
    nested['status'] = 'completed'
    nested['child_result'] = ChildAgentResult(task_id=nested['session_id'], name='nested', status='completed', final_text='nested result', session_id=nested['session_id'], parent_session_id=source['session_id'], agent_id=nested['agent_id']).to_dict()
    subagent._save_subagent_state(source['session_id'], nested, tree)
    nested_native = Session.create(nested['session_id'], [{'role': 'user', 'content': 'nested history'}], workspace=tree, parent_session_id=source['session_id'])
    asyncio.run(LegacyJsonSessionStore().save(nested_native))
    native = asyncio.run(LegacyJsonSessionStore().load(SessionIdentity(source['session_id'], 'old-parent'), tree))
    reference = _reference(nested['session_id'])
    reference['_nz_parts'][0]['state']['metadata'].update(nested['child_result'] and {'child_result': nested['child_result'], 'child_agent_id': nested['agent_id'], 'child_parent_session_id': source['session_id']})
    from nz_coder.protocol.message_schema import attach_message_identity
    attach_message_identity(reference, 'msg-reference', session_id=source['session_id'])
    reference['_nz_parts'][0].update(id='part-reference', message_id='msg-reference')
    native.append(reference)
    asyncio.run(LegacyJsonSessionStore().save(native))
    result = subagent.clone_referenced_subagents('old-parent', 'new-parent', [_reference(source['session_id'])], parent_agent_id='new-parent-agent', workspace_root=tmp_path)
    target_id = result.mapping[source['session_id']]
    nested_id = result.mapping[nested['session_id']]
    target = subagent._load_subagent_state('new-parent', target_id, tmp_path)
    nested_target = subagent.load_subagent_session(target_id, nested_id, Path(target['worktree']['path']))
    assert nested_target['messages'][0]['content'] == 'nested history'
    assert nested_target['parent_agent_id'] == target['agent_id']
    assert nested_target['child_result']['parent_session_id'] == target_id
    assert nested_target['child_result']['task_id'] == nested_id
    target_display = subagent.load_subagent_session('new-parent', target_id, tmp_path)
    metadata = target_display['messages'][-1]['_nz_parts'][0]['state']['metadata']
    assert metadata['child_session_id'] == nested_id
    assert metadata['child_parent_session_id'] == target_id
    assert metadata['child_result']['session_id'] == nested_id


def test_cloned_native_child_continues_through_real_run_subagent(tmp_path, monkeypatch):
    from tests.test_subagent import FakeClient, FakeResponse, FakeMessage
    source, tree = _native_source(tmp_path)
    result = subagent.clone_referenced_subagents('old-parent', 'new-parent', [_reference(source['session_id'])], workspace_root=tmp_path)
    target_id = result.mapping[source['session_id']]
    client = FakeClient([FakeResponse(FakeMessage('continued findings'))])
    monkeypatch.setattr(subagent, 'OpenAI', lambda **kwargs: client)
    with scoped_workdir(tmp_path), subagent.scoped_parent_context(session_id='new-parent'):
        outcome = subagent.run_subagent('continue the original task', session_id=target_id)
    assert outcome.metadata['child_status'] == 'completed_unverified'
    request = client.chat.completions.requests[0]['messages']
    conversation = [m['content'] for m in request if m['role'] != 'system']
    assert conversation[:3] == ['remember this', 'original findings', 'continue the original task']
    assert conversation.count('continue the original task') == 1
    assert all(text.startswith('<context-injection>') for text in conversation[3:])
    assert subagent.load_subagent_session('old-parent', source['session_id'], tmp_path)['messages'][-1]['content'] == 'original findings'
    assert 'messages' not in subagent._load_subagent_state('new-parent', target_id, tmp_path)


@pytest.mark.parametrize('artifact_field', ['session_artifact', 'task_artifact'])
def test_clone_cleanup_reports_delete_failure_and_retries_whole_batch(tmp_path, monkeypatch, artifact_field):
    import os

    sources = [_snapshot_source(tmp_path), _snapshot_source(tmp_path)]
    original_histories = [
        subagent.load_subagent_session('old-parent', source['session_id'], tmp_path)['messages']
        for source, _, _ in sources
    ]
    result = subagent.clone_referenced_subagents(
        'old-parent', 'new-parent', [_reference(source['session_id']) for source, _, _ in sources],
        workspace_root=tmp_path,
    )
    failed = result.created[-1]
    failed_directory = getattr(failed, artifact_field)
    original_rmdir = os.rmdir

    def fail_owned_directory(path, *args, **kwargs):
        if Path(path) == failed_directory:
            raise PermissionError(13, 'clone cleanup denied', str(path))
        return original_rmdir(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(os, 'rmdir', fail_owned_directory)
        with pytest.raises(PermissionError, match='clone cleanup denied'):
            subagent.discard_cloned_subagents(result)
    assert failed_directory.exists()
    assert all(not resource.discarded for resource in result.created)

    subagent.discard_cloned_subagents(result)
    subagent.discard_cloned_subagents(result)
    for resource in result.created:
        assert resource.discarded
        assert not resource.session_file.exists()
        assert not resource.session_artifact.exists()
        assert not resource.task_artifact.exists()
        assert not Path(resource.state['worktree']['path']).exists()
    for (source, tree, snapshots), history in zip(sources, original_histories):
        assert (tree / 'app.py').read_text() == 'after\n'
        assert snapshots.root.exists()
        assert subagent.load_subagent_session('old-parent', source['session_id'], tmp_path)['messages'] == history
