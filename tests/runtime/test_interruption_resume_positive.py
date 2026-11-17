"""硬退出仅承诺恢复已提交检查点，不重建未落盘的最后一步。"""

import asyncio
import copy
import sys
import threading
from dataclasses import replace
from nz_coder.runtime.execution import native_sdk
from settlement_scenarios import setup, payload, _reply, _call


def test_cancel_then_fresh_environment_resumes_durable_history(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    calls = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            calls.append(copy.deepcopy(kwargs))
            n = len(calls)
            if n == 1:
                return _reply(
                    kwargs,
                    calls=[
                        _call(
                            1, "write_file", {"path": "marker.txt", "content": "ready"}
                        )
                    ],
                )
            if n == 2:
                entered.set()
                assert release.wait(5)
                return _reply(kwargs, text="late answer")
            return _reply(
                kwargs, text="Existing marker retained; read-only continuation."
            )

    request, options = setup(monkeypatch, tmp_path, Provider())
    request = replace(
        request,
        messages=[
            {
                "role": "user",
                "content": "Create marker.txt containing ready. Preserve the original result on continuation.",
            }
        ],
        tool_names=("write_file",),
    )
    runner = native_sdk.NativeSDKRunner()

    async def run():
        task = asyncio.create_task(runner.run_result(request, options))
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        release.set()
        try:
            cancelled = await task
        except asyncio.CancelledError:
            pass
        else:
            assert cancelled.status.value == "cancelled"
        assert (tmp_path / "marker.txt").read_text() == "ready"
        continued = replace(
            request, messages=[{"role": "user", "content": "继续"}], tool_names=()
        )
        result = await runner.run_result(continued, options)
        assert result.status.value in {"completed", "completed_unverified"}
        assert len(calls) == 3
        assert "Create marker.txt" in str(calls[2]["messages"])
        assert (tmp_path / "marker.txt").read_text() == "ready"
        assert payload(tmp_path, request)["run_status"] in {
            "completed",
            "completed_unverified",
        }

    try:
        asyncio.run(run())
    finally:
        release.set()
        runner.retry_cleanup(tmp_path, request.session_id)


def test_hard_process_exit_then_resume_from_durable_checkpoint(monkeypatch, tmp_path):
    import subprocess
    import os
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    child = tmp_path / "child_harness.py"
    child.write_text("""
import asyncio, os, sys
from dataclasses import replace
from pathlib import Path
import pytest
from settlement_scenarios import setup, _reply, _call, native_sdk
class Provider:
    name='offline'
    count=0
    def create_completion(self,_client,**kwargs):
        self.count+=1
        if self.count==1:return _reply(kwargs,calls=[_call(1,'write_file',{'path':'marker.txt','content':'durable-before-crash'})])
        os._exit(77)
mp=pytest.MonkeyPatch()
request,options=setup(mp,Path(sys.argv[1]),Provider())
request=replace(request,messages=[{'role':'user','content':'Create marker.txt; preserve it when resuming.'}],tool_names=('write_file',))
asyncio.run(native_sdk.NativeSDKRunner().run_result(request,options))
""")
    completed = subprocess.run(
        [sys.executable, "-B", str(child), str(tmp_path)],
        cwd=repo,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "tests/runtime"))),
        },
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 77, completed.stderr
    assert (tmp_path / "marker.txt").read_text() == "durable-before-crash"
    requests = []

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            requests.append(copy.deepcopy(kwargs))
            return _reply(
                kwargs, text="Existing committed file retained after restart."
            )

    request, options = setup(monkeypatch, tmp_path, Provider())
    request = replace(
        request, messages=[{"role": "user", "content": "继续"}], tool_names=()
    )
    result = asyncio.run(native_sdk.NativeSDKRunner().run_result(request, options))
    assert result.status.value in {"completed", "completed_unverified"}
    assert len(requests) == 1 and "Create marker.txt" in str(requests[0]["messages"])
    assert (tmp_path / "marker.txt").read_text() == "durable-before-crash"
