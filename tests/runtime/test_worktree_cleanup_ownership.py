"""Worktree cleanup preserves uncertain or redirected directories."""
from types import SimpleNamespace
import subprocess
import pytest
from nz_coder.runtime.worktree import Worktree, WorktreeManager
from nz_coder.runtime.worktree.manager import WorktreeError
from nz_coder.runtime.workflows.workflow_sweep import sweep_workflow_worktrees


def test_remove_internal_symlink_retains_victim(tmp_path):
    managed = tmp_path / 'worktrees'
    victim = managed / 'victim'
    victim.mkdir(parents=True)
    (victim / 'important').write_text('keep')
    alias = managed / 'alias'
    alias.symlink_to(victim, target_is_directory=True)
    manager = WorktreeManager(tmp_path, worktree_dir=managed)
    with pytest.raises(WorktreeError):
        manager.remove(Worktree('alias', str(alias), '', 'HEAD', '', mode='copy'))
    assert (victim / 'important').read_text() == 'keep'


def test_sweep_git_status_failure_retains_unmerged_tree_with_warning(tmp_path, monkeypatch):
    managed = tmp_path / 'cache' / 'worktrees'
    target = managed / 'dirty'
    target.mkdir(parents=True)
    (target / 'unmerged').write_text('keep')
    monkeypatch.setattr('nz_coder.runtime.worktree.manager.prepare_user_storage',
                        lambda root: SimpleNamespace(workspace_cache=tmp_path / 'cache'))
    monkeypatch.setattr(WorktreeManager, '_run_git', lambda *a, **kw: subprocess.CompletedProcess(['git'], 1, '', 'unavailable'))
    state = {'session_id':'dirty', 'workflow_run_id':'run', 'status':'completed', 'changed_files':[],
             'worktree':{'id':'dirty', 'path':str(target), 'mode':'git', 'branch':'', 'based_on':'HEAD'}}
    result = sweep_workflow_worktrees([state], tmp_path)
    assert result['removed'] == []
    assert result['warnings']
    assert (target / 'unmerged').read_text() == 'keep'


def test_cleanup_directory_swap_does_not_delete_replacement(tmp_path, monkeypatch):
    managed = tmp_path / 'worktrees'
    target = managed / 'child'
    target.mkdir(parents=True)
    (target / 'original').write_text('old')
    manager = WorktreeManager(tmp_path, worktree_dir=managed)
    original = manager._remove_directory
    def swap(path, *args, **kwargs):
        path.rename(managed / 'moved')
        path.mkdir()
        (path / 'replacement').write_text('keep')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(manager, '_remove_directory', swap)
    with pytest.raises(WorktreeError):
        manager.remove(Worktree('child', str(target), '', 'HEAD', '', mode='copy'))
    assert (target / 'replacement').read_text() == 'keep'


def test_confirmed_clean_git_tree_can_be_removed(tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    repo.mkdir()
    def git(*args):
        return subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True, text=True)
    git('init')
    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Test')
    (repo / 'app').write_text('content')
    git('add', 'app')
    git('commit', '-m', 'initial')
    managed = tmp_path / 'worktrees'
    managed.mkdir()
    target = managed / 'child'
    git('worktree', 'add', '-b', 'subagent-child', str(target))
    manager = WorktreeManager(repo, worktree_dir=managed)
    assert manager.changed_files(target) == []
    assert manager.remove(Worktree('child', str(target), 'subagent-child', 'HEAD', '', mode='git'))
    assert not target.exists()
    assert str(target) not in git('worktree', 'list', '--porcelain').stdout
    git('worktree', 'add', '-b', 'subagent-second', str(managed / 'second'))
    monkeypatch.setattr('nz_coder.runtime.worktree.manager.prepare_user_storage',
                        lambda root: SimpleNamespace(workspace_cache=tmp_path))
    state = {'session_id': 'second', 'workflow_run_id': 'run', 'status': 'completed',
             'changed_files': [], 'worktree': {'id': 'second', 'path': str(managed / 'second'),
             'mode': 'git', 'branch': 'subagent-second', 'based_on': 'HEAD'}}
    result = sweep_workflow_worktrees([state], repo)
    assert result['removed'] == [str(managed / 'second')]
    assert result['warnings'] == []
    assert not (managed / 'second').exists()


def test_git_mode_cleanup_does_not_fallback_when_git_unavailable(tmp_path, monkeypatch):
    managed = tmp_path / 'worktrees'
    target = managed / 'child'
    target.mkdir(parents=True)
    (target / 'important').write_text('keep')
    manager = WorktreeManager(tmp_path, worktree_dir=managed)
    monkeypatch.setattr(manager, '_run_git', lambda *a, **kw: subprocess.CompletedProcess(['git'], 1, '', 'unavailable'))
    with pytest.raises(WorktreeError):
        manager.remove(Worktree('child', str(target), '', 'HEAD', '', mode='git'))
    assert (target / 'important').read_text() == 'keep'
