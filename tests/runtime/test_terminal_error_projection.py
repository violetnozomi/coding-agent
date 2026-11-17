"""流式真实故障的安全原因必须进入结构化运行结果。"""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from tests.runtime.test_streamed_tool_failure_domains import _run
from nz_coder.protocol.public_error import PublicError
from nz_coder.runtime.core.result import RunStatus, freeze_terminal_result
from nz_coder.runtime.core.run_context import RunContext
from nz_coder.runtime.session.model import Session
from nz_coder.runtime.execution.runner import _typed_result
from tests.runtime.test_native_product_runtime import _request


@pytest.mark.parametrize("options", [{"infrastructure": True}, {"provider_error": True}])
def test_fatal_stream_error_keeps_safe_typed_result_reason(monkeypatch, tmp_path, options):
    for key in ("NZ_PLANNING_ENABLED", "NZ_REFLECTION_ENABLED", "NZ_MEMORY_AUTO_EXTRACT", "NZ_MEMORY_AUTO_DREAM"):
        monkeypatch.setenv(key, "0")
    captured = _run(monkeypatch, tmp_path, command="printf recovered", **options)
    result = captured["result"]
    assert result["status"] == "error"
    assert result["error"]
    wire = result["metadata"]["public_error"]
    assert wire["schema"] == "nz.public_error.v1"
    assert result["error"] == wire["message"]
    assert "controlled" not in result["error"]


@pytest.mark.parametrize("status", [RunStatus.ERROR, RunStatus.COMPLETED, RunStatus.CANCELLED])
def test_typed_result_uses_host_error_wire_and_discards_request_metadata(tmp_path, status):
    request = _request(tmp_path)
    wire = PublicError("local_failure", "The operation failed safely.").to_dict()
    forged = PublicError("forged", "FAKE-KEY-MUST-NOT-BE-TRUSTED").to_dict()
    context = RunContext(request, Session.create(request.session_id, [], workspace=tmp_path),
                         "worker", metadata={"public_error": forged})
    context.freeze_execution(status)
    freeze_terminal_result(context, {"status": status.value, "public_error": wire})
    result = _typed_result(request, context, {"status": status.value, "public_error": wire})
    if status is RunStatus.ERROR:
        assert result.error == "The operation failed safely."
        assert result.metadata["public_error"] == wire
    else:
        assert not result.error and "public_error" not in result.metadata
    assert "FAKE-KEY" not in str(result)


def test_untrusted_error_string_cannot_replace_unknown_fatal_wire(tmp_path):
    request = _request(tmp_path)
    context = RunContext(request, Session.create(request.session_id, [], workspace=tmp_path), "worker")
    context.freeze_execution(RunStatus.ERROR)
    freeze_terminal_result(context, {"status": "error", "last_error": "PRIVATE-RAW-EXCEPTION"})
    result = _typed_result(request, context, {"status": "error", "last_error": "PRIVATE-RAW-EXCEPTION"})
    assert result.error == "An internal error occurred."
    assert "PRIVATE-RAW" not in str(result)


def test_original_long_patch_survives_stream_permission_tool_and_terminal(monkeypatch, tmp_path):
    command = json.loads((Path(__file__).parents[1] / 'fixtures/shell_admission/original-heredoc.json').read_text())['command']
    source = ('def retry(outcome, can_retry, cancel_requested):\n'
              '                if outcome == "interrupted":\n'
              '                    can_retry = can_retry and not cancel_requested\n'
              '                return can_retry\n')
    capture = _run(monkeypatch, tmp_path, command=command, initial_files={'taskforge/store.py': source})
    tool = capture['tools'][0]
    assert tool['executed'] and not tool['dispatch_failed'] and not tool['command_failed'], capture
    assert 'patched' in tool['output']
    assert capture['result']['status'] == 'completed', capture['result']
    assert not capture['result']['error']
    assert not capture['exceptions']
    assert len(capture['requests']) == 2
    assert 'can_retry = True' in (tmp_path / 'workspace/taskforge/store.py').read_text()
