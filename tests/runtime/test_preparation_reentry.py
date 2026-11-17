"""启动尚未进入执行时，失败只撤销本次绑定，不伪造终态或破坏旧文件。"""

import asyncio
import pytest

from nz_coder.runtime.execution import native_sdk
from nz_coder.runtime.execution import loop
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state import sessions
from settlement_scenarios import setup, _reply


def environment(monkeypatch, tmp_path):
    calls = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(kwargs)
            return _reply(kwargs, text="Read-only retry complete.")

    request, options = setup(monkeypatch, tmp_path, Provider())
    env = native_sdk.build_product_run_environment(
        request, options, config_snapshot=options.config_snapshot
    )
    return request, options, env, native_sdk.NativeSDKRunner(env), calls


def test_repaired_restore_can_retry_same_environment(monkeypatch, tmp_path):
    from nz_coder.runtime.verification.domain.checkpoint import CheckpointRestoreError

    request, options, env, runner, calls = environment(monkeypatch, tmp_path)
    with scoped_workdir(tmp_path):
        path = sessions.session_runtime_state_path(request.session_id)
        env.runtime_state.save(path, active=True)
    original = path.read_bytes()
    path.write_text('{"verification_domain":')
    broken = path.read_bytes()
    try:
        with pytest.raises(CheckpointRestoreError):
            asyncio.run(runner.run_result(request, options))
        assert not calls and path.read_bytes() == broken
        assert env.runtime_state._verification_context is None
        path.write_bytes(original)
        result = asyncio.run(runner.run_result(request, options))
        assert result.status.value in {"completed", "completed_unverified"}
        assert len(calls) == 1
    finally:
        env.close()


@pytest.mark.parametrize("after_writer", [False, True])
def test_factory_failure_releases_only_new_owner(monkeypatch, tmp_path, after_writer):
    request, options, env, runner, calls = environment(monkeypatch, tmp_path)
    factory = loop.runner_context_from_legacy_host
    failed = []

    def broken(host, services, context):
        failed.append(context)
        if after_writer:
            factory(host, services, context)
        raise ValueError("port construction failed")

    monkeypatch.setattr(loop, "runner_context_from_legacy_host", broken)
    try:
        with pytest.raises(ValueError, match="port construction"):
            asyncio.run(runner.run_result(request, options))
        assert not calls and not failed[0].finalized
        assert env.runtime_state._verification_context is None
        assert env.active_run_context is None
        assert env.verification_runtime is None
        if after_writer:
            assert env._session_checkpoint_writer.retired
        monkeypatch.setattr(loop, "runner_context_from_legacy_host", factory)
        result = asyncio.run(runner.run_result(request, options))
        assert result.status.value in {"completed", "completed_unverified"}
        assert len(calls) == 1
        successor = env.runtime_state._verification_context
        env._abort_run_preparation(failed[0])
        assert env.runtime_state._verification_context is successor
    finally:
        env.close()
