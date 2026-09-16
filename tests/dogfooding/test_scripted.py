"""The offline transport fixture calls real tools, never claims coding success."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def test_scripted_permission_request_then_acknowledges_real_tool_result():
    path = Path(__file__).parent / "provider/r1_scripted.py"
    assert path.exists(), "Offline product transport fixture missing"
    spec = importlib.util.spec_from_file_location("r1_scripted", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adapter = module.factory(provider_name="r1-scripted", api_key="dummy", base_url="http://127.0.0.1")
    messages = [{"role":"user", "content":"R1:F01 reject"}]
    result = adapter.create_completion(None, messages=messages, model="fixture", stream=False)
    call = result.choices[0].message.tool_calls[0]
    assert call.function.name == "write_file"
    messages += [{"role":"assistant", "content":None, "tool_calls":[call.model_dump()]},
                 {"role":"tool", "tool_call_id":call.id, "content":"Error: Permission denied"}]
    result = adapter.create_completion(None, messages=messages, model="fixture", stream=False)
    assert "denied" in result.choices[0].message.content
    assert not result.choices[0].message.tool_calls
