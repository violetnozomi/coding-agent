"""R01–R08 的离线真实行为回归。"""
from __future__ import annotations

import email.message
import io
import json
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path
from dataclasses import replace

import pytest

from nz_coder.foundation.user_paths import prepare_user_storage
from nz_coder.foundation.workspace_trust import load_config_snapshot
from nz_coder.intelligence.code_index import PersistentCodeIndex
from nz_coder.intelligence.repository_graph import RepositoryGraph
from nz_coder.intelligence.service import RepoIntelligenceService
from nz_coder.mcp.client import MCPClient, MCPError, MCPTimeoutError
from nz_coder.mcp.config import load_mcp_server_configs
from nz_coder.mcp.http_client import MCPHTTPClient
from nz_coder.mcp.runtime import MCPRuntime
from nz_coder.project_creation.blueprint import create_project_blueprint
from nz_coder.project_creation.completeness import check_project_completeness
from nz_coder.project_creation.inspector import inspect_generated_project
from nz_coder.project_creation.requirement_analyzer import analyze_project_requirements
from nz_coder.project_creation.templates import TemplateRegistry
from nz_coder.runtime.process.workdir import scoped_workdir


def _snapshot(root, raw, tmp_path):
    return load_config_snapshot(root, environ={
        'NZ_MCP_SERVERS_JSON': json.dumps(raw),
        'NZ_MCP_USER_CONFIG': str(tmp_path.parent / (tmp_path.name + '-absent.json')),
        'NZ_MCP_TRUST_STORE': str(tmp_path.parent / (tmp_path.name + '-trust.json')),
    }, user_config_path=tmp_path / 'absent.env')


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX shebang')
def test_mcp_shebang_workspace_interpreter_requires_trust(tmp_path):
    root = tmp_path / 'workspace'
    (root / 'bin').mkdir(parents=True)
    interpreter = root / 'bin/node'
    marker = tmp_path / 'untrusted-started'
    interpreter.write_text('#!/bin/sh\nprintf SENTINEL > ' + str(marker) + '\n')
    interpreter.chmod(0o755)
    script = tmp_path / 'server'
    script.write_text('#!/usr/bin/env node\n// harmless fixture\n')
    script.chmod(0o755)
    server = load_mcp_server_configs(workspace=root, config_snapshot=_snapshot(
        root, {'local': {'command': [str(script)], 'env': {'PATH': str(root / 'bin')}}}, tmp_path,
    ))[0]
    runtime = MCPRuntime([server])
    try:
        runtime.start()
        assert runtime.status_summary()[0]['status'] == 'untrusted'
        assert not marker.exists()
        assert server.execution_identity.workspace_controlled
        assert not server.trusted
    finally:
        runtime.close()


@pytest.mark.skipif(sys.platform == 'win32', reason='POSIX shebang')
def test_mcp_launch_uses_frozen_interpreter_and_environment(tmp_path, monkeypatch):
    root = tmp_path / 'workspace'
    root.mkdir()
    script = tmp_path / 'echo-server'
    fixture = Path(__file__).parent / 'fixtures/mcp_echo_server.py'
    observed = tmp_path / 'observed-lang'
    content = fixture.read_text().replace('import time',
        'import time\nimport os\nfrom pathlib import Path\n'
        + f'Path({str(observed)!r}).write_text(os.environ.get("LANG", ""))')
    script.write_text('#!/usr/bin/env python3\n' + content)
    script.chmod(0o755)
    server = load_mcp_server_configs(workspace=root, config_snapshot=_snapshot(
        root, {'echo': {'command': [str(script)], 'env': {'LANG': 'C'}}}, tmp_path,
    ))[0]
    frozen = server.execution_identity.environment
    assert frozen is not None
    monkeypatch.setenv('PATH', str(root / 'missing'))
    monkeypatch.setenv('LANG', 'changed-after-discovery')
    runtime = MCPRuntime([server])
    try:
        runtime.start()
        assert runtime.status_summary()[0]['status'] == 'connected'
        client = runtime.clients['echo']
        assert client.process.args[0] == server.execution_identity.launch_command[0]
        assert client.process.args[0] != str(script)
        assert observed.read_text() == 'C'
    finally:
        runtime.close()


def test_mcp_changed_identity_does_not_spawn(tmp_path, monkeypatch):
    script = tmp_path / 'server.py'
    script.write_text('print(1)\n')
    server = load_mcp_server_configs(workspace=tmp_path, config_snapshot=_snapshot(
        tmp_path, {'local': {'command': [sys.executable, str(script)]}}, tmp_path,
    ))[0]
    script.write_text('print(2)\n')
    def unexpected_spawn(*args, **kwargs):
        pytest.fail('changed identity reached Popen')
    monkeypatch.setattr('nz_coder.mcp.client.subprocess.Popen', unexpected_spawn)
    client = MCPClient(name='local', command=server.command, cwd=server.cwd,
                       execution_identity=server.execution_identity)
    with pytest.raises(MCPError, match='identity changed'):
        client.start()


def test_index_ignores_legacy_external_database(tmp_path):
    root = tmp_path / 'workspace'
    legacy = root / '.nz-coder/index'
    legacy.mkdir(parents=True)
    external = tmp_path / 'external.sqlite3'
    with sqlite3.connect(external) as db:
        db.execute('CREATE TABLE files(sentinel TEXT)')
        db.execute("INSERT INTO files VALUES ('KEEP')")
        db.execute('PRAGMA user_version=1')
    (legacy / 'code-index.sqlite3').symlink_to(external)
    index = PersistentCodeIndex(root)
    with sqlite3.connect(external) as db:
        assert db.execute('SELECT sentinel FROM files').fetchall() == [('KEEP',)]
    assert index.database_path == prepare_user_storage(root).workspace_cache / 'indexes/code-index.sqlite3'


@pytest.mark.parametrize('entry', ['indexes', 'code-index.sqlite3', 'code-index.sqlite3-wal',
                                  'code-index.sqlite3-shm', 'code-index.sqlite3-journal'])
@pytest.mark.parametrize('dangling', [False, True])
def test_index_rejects_private_cache_aliases(tmp_path, entry, dangling):
    root = tmp_path / 'workspace'
    root.mkdir()
    cache = prepare_user_storage(root).workspace_cache
    indexes = cache / 'indexes'
    external = tmp_path / 'external'
    if not dangling:
        if entry == 'indexes':
            external.mkdir()
            (external / 'sentinel').write_text('KEEP')
        else:
            external.write_text('KEEP')
    if entry == 'indexes':
        indexes.symlink_to(external, target_is_directory=True)
    else:
        indexes.mkdir()
        (indexes / entry).symlink_to(external)
    with pytest.raises(ValueError):
        PersistentCodeIndex(root)
    if not dangling:
        assert (external / 'sentinel' if external.is_dir() else external).read_text() == 'KEEP'


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_index_rechecks_aliases_between_connections(tmp_path, suffix):
    index = PersistentCodeIndex(tmp_path)
    external = tmp_path.parent / (tmp_path.name + '-external')
    external.write_text('KEEP')
    target = Path(str(index.database_path) + suffix)
    if target.exists():
        target.unlink()
    target.symlink_to(external)
    with pytest.raises(ValueError):
        index.snapshot()
    assert external.read_text() == 'KEEP'


def test_index_rechecks_directory_alias_between_connections(tmp_path):
    index = PersistentCodeIndex(tmp_path)
    directory = index.database_path.parent
    backup = directory.with_name("original-indexes")
    directory.rename(backup)
    external = tmp_path.parent / (tmp_path.name + "-external-dir")
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("KEEP")
    directory.symlink_to(external, target_is_directory=True)
    with pytest.raises(ValueError):
        index.snapshot()
    assert sentinel.read_text() == "KEEP"
    assert sorted(item.name for item in external.iterdir()) == ["sentinel"]


def test_explicit_index_workspace_does_not_follow_context(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    with scoped_workdir(a):
        index = PersistentCodeIndex(b)
    assert index.database_path == prepare_user_storage(b).workspace_cache / 'indexes/code-index.sqlite3'


def test_symbol_limit_does_not_hide_ambiguity(tmp_path):
    for name in ['a.py', 'b.py']:
        (tmp_path / name).write_text('def shared():\n    return 1\n')
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)
    result = index.symbol_context('shared', 1)
    assert result['ambiguous']
    assert result['definition'] is None
    assert len(result['alternatives']) == 1
    assert result['references'] == result['callers'] == result['callees'] == []


def test_graph_resolves_python_relative_import_levels_and_packages(tmp_path):
    files = {
        'pkg/__init__.py': 'from .b import VALUE\n',
        'pkg/b.py': 'VALUE = 1\n',
        'pkg/a.py': 'from .b import VALUE\n',
        'pkg/sub/__init__.py': 'from ..b import VALUE\n',
        'pkg/sub/a.py': 'from ..b import VALUE\n',
        'pkg/use.py': 'from .sub import VALUE\n',
    }
    for name, content in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    index = PersistentCodeIndex(tmp_path)
    index.scan(tmp_path, max_files=20)
    graph = RepositoryGraph(tmp_path, index=index)
    graph.build(snapshot=index.snapshot())
    for source in ['pkg/__init__.py', 'pkg/a.py', 'pkg/sub/__init__.py', 'pkg/sub/a.py']:
        assert graph._graph[source] == ('pkg/b.py',)
    assert graph._graph['pkg/use.py'] == ('pkg/sub/__init__.py',)
    dependents = graph.changed_scope(changed_paths=['pkg/b.py'])['dependent_modules']
    assert 'pkg/a.py' in dependents


def test_removed_workspace_settles_poll_watcher_state(tmp_path):
    root = tmp_path / 'workspace'
    root.mkdir()
    service = RepoIntelligenceService(root)
    try:
        service.prewarm(max_files=20).result(timeout=5)
        service._state = replace(service.state, watcher_backend='poll')
        shutil.rmtree(root)
        service._poll_watch_loop(.001, 0, 20, {})
        assert service.state.watcher_backend == 'none'
    finally:
        service.close()


def _scaffold(root, name, kind):
    project = root / name
    project.mkdir()
    for item in TemplateRegistry.build(name, kind)['files']:
        target = project / item['path']
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item['content'])
    return project


def test_completeness_keeps_every_unchecked_feature_visible(tmp_path):
    spec = analyze_project_requirements('Build a word counter CLI named word_counter with pytest and README')
    spec['features'].append('custom requested behavior')
    _scaffold(tmp_path, spec['project_name'], spec['project_type'])
    with scoped_workdir(tmp_path):
        result = check_project_completeness(spec, create_project_blueprint(spec), spec['project_name'])
    assert result['status'] != 'ok'
    assert set(result['feature_status']) == set(spec['features'])
    assert result['feature_status']['word counting'] == 'unverified'
    assert result['feature_status']['custom requested behavior'] == 'unverified'
    assert 'word counting' in result['unverified']
    assert any('verif' in step.lower() for step in result['recommended_next_steps'])


@pytest.mark.parametrize('kind', ['rag_demo', 'agent_demo'])
def test_demo_inspector_uses_template_test_entry(tmp_path, kind):
    _scaffold(tmp_path, kind, kind)
    with scoped_workdir(tmp_path):
        result = inspect_generated_project(kind, kind)
    assert result['project_type'] == kind
    assert result['checks']['pytest_tests']
    (tmp_path / kind / f'tests/test_{kind}.py').unlink()
    with scoped_workdir(tmp_path):
        assert not inspect_generated_project(kind, kind)['checks']['pytest_tests']


def test_http_waiting_for_lock_consumes_deadline_and_never_dispatches():
    class Response(io.BytesIO):
        status = 200
        headers = email.message.Message()
        headers['Content-Type'] = 'application/json'
    dispatched = []
    class Opener:
        def open(self, request, **kwargs):
            dispatched.append(request)
            return Response(b'{"jsonrpc":"2.0","id":0,"result":{}}')
    client = MCPHTTPClient(name='deadline', url='https://offline.invalid/mcp')
    client._opener = Opener()
    client._request_lock.acquire()
    release = threading.Timer(.2, client._request_lock.release)
    release.start()
    started = time.monotonic()
    try:
        with pytest.raises(MCPTimeoutError):
            client.request('probe', timeout=.03)
        assert time.monotonic() - started < .15
        assert dispatched == []
    finally:
        release.join()
        client.close()
