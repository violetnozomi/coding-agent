"""Tests for local MCP stdio configuration, transport, and scoped tools."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nz_coder.mcp import (
    MCPClient,
    MCPError,
    MCPRequestError,
    MCPRuntime,
    MCPServerConfig,
    MCPTimeoutError,
    load_mcp_server_configs,
)
from nz_coder.tool_platform.permissioning.checker import PermissionChecker
from nz_coder.tool_platform.permissioning.rules import PermissionRule
from nz_coder.runtime.execution.tool_executor import is_transactional_write_tool
from nz_coder.permissions import PermissionManager
from nz_coder.runtime.execution.tool_executor import ToolExecutor
from nz_coder.tools import (
    dispatch,
    get_execution_mode,
    get_specs,
    scoped_dynamic_tool_provider,
    scoped_dynamic_tools,
    scoped_dynamic_tools_disabled,
)


FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
ORPHAN_SERVER = Path(__file__).parent / "fixtures" / "mcp_orphan_server.py"


def _server_config(tmp_path: Path, *, name: str = "echo") -> MCPServerConfig:
    return MCPServerConfig(
        name=name,
        command=(sys.executable, str(FIXTURE_SERVER)),
        cwd=tmp_path,
        startup_timeout_seconds=3,
        tool_timeout_seconds=1,
        tool_effects=(("echo", "read"), ("fail", "write")),
    )


def test_mcp_config_validates_command_workspace_and_effects(tmp_path):
    child = tmp_path / "service"
    child.mkdir()
    configs = load_mcp_server_configs(
        {
            "servers": {
                "local": {
                    "command": ["python3", "server.py"],
                    "cwd": "service",
                    "env": {"MCP_MODE": "test"},
                    "startup_timeout_seconds": 2,
                    "tool_timeout_seconds": 4,
                    "tool_effects": {"lookup": "read", "update": "write"},
                }
            }
        },
        workspace=tmp_path,
    )

    assert configs == [
        MCPServerConfig(
            name="local",
            command=("python3", "server.py"),
            cwd=child,
            environment=(("MCP_MODE", "test"),),
            startup_timeout_seconds=2,
            tool_timeout_seconds=4,
            tool_effects=(("lookup", "read"), ("update", "write")),
        )
    ]
    assert configs[0].effect_for("undeclared") == "serial"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({"bad": {"command": "python server.py"}}, "string array"),
        ({"bad": {"command": ["python3"], "cwd": "../escape"}}, "escapes workspace"),
        ({"bad": {"command": ["python3"], "tool_effects": {"x": "safe"}}}, "one of"),
        ({"servers": {}, "extra": True}, "wrapper has unknown"),
        (["not", "an", "object"], "JSON text or an object"),
        ({"bad": {"command": ["python3"], "tool_timeout_seconds": True}}, "finite"),
        ({"bad": {"command": ["python3"], "tool_timeout_seconds": float("nan")}}, "finite"),
        ({"bad": {"command": ["python3"], "tool_timeout_seconds": float("inf")}}, "finite"),
    ],
)
def test_mcp_config_rejects_unsafe_or_ambiguous_values(tmp_path, raw, expected):
    with pytest.raises(ValueError, match=expected):
        load_mcp_server_configs(raw, workspace=tmp_path)


@pytest.mark.parametrize("timeout", [0, -1, True, float("inf"), float("nan")])
def test_mcp_client_rejects_invalid_timeout_before_spawn(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        MCPClient(
            name="invalid",
            command=(sys.executable, str(FIXTURE_SERVER)),
            cwd=tmp_path,
            startup_timeout_seconds=timeout,
        )


def test_mcp_client_initializes_lists_calls_times_out_and_closes(tmp_path):
    client = MCPClient(
        name="echo",
        command=(sys.executable, str(FIXTURE_SERVER)),
        cwd=tmp_path,
        startup_timeout_seconds=3,
        tool_timeout_seconds=0.05,
    )
    result = client.start()

    assert result["serverInfo"]["name"] == "test-echo"
    assert [tool["name"] for tool in client.list_tools()] == [
        "echo",
        "fail",
        "structured",
        "delay",
    ]
    assert client.call_tool("echo", {"value": "hello"})["content"][0]["text"] == "echo:hello"
    assert client.call_tool("structured", {})["structuredContent"] == {"answer": 42}
    with pytest.raises(MCPError, match="valid JSON"):
        client.call_tool("echo", {"value": float("nan")})
    assert client._pending == {}
    assert [item["name"] for item in client.list_prompts()] == ["review"]
    assert client.get_prompt("review", {"topic": "MCP"})["messages"][0][
        "content"
    ]["text"] == "Review MCP"
    assert [item["uri"] for item in client.list_resources()] == ["test://guide"]
    assert client.read_resource("test://guide")["contents"][0]["text"] == (
        "guide-body"
    )
    with pytest.raises(MCPTimeoutError, match="timed out"):
        client.call_tool("delay", {"seconds": 0.2})
    with pytest.raises(MCPRequestError) as error:
        client.request("missing/method", {}, timeout=1)
    assert error.value.code == -32601

    process = client.process
    client.close()
    assert process is not None
    assert process.poll() is not None


def test_mcp_client_cannot_start_after_close(tmp_path):
    client = MCPClient(
        name="closed",
        command=(sys.executable, str(FIXTURE_SERVER)),
        cwd=tmp_path,
    )
    client.close()

    with pytest.raises(MCPError, match="is closed"):
        client.start()

    assert client.process is None


def test_mcp_client_replays_list_change_received_before_handler(tmp_path):
    client = MCPClient(
        name="buffered",
        command=("unused",),
        cwd=tmp_path,
    )
    received = threading.Event()
    worker = threading.Thread(target=client._dispatch_notifications, daemon=True)
    worker.start()
    try:
        client._queue_notification(
            "notifications/tools/list_changed",
            {"generation": 1},
        )
        client.set_notification_handler(
            "notifications/tools/list_changed",
            lambda _params: received.set(),
        )
        assert received.wait(timeout=1)
    finally:
        client._closed = True
        client._notification_queue.put_nowait(("unused", {}))
        worker.join(timeout=1)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_mcp_close_kills_descendant_after_server_leader_exits(tmp_path):
    pid_path = tmp_path / "child.pid"
    client = MCPClient(
        name="orphan",
        command=(sys.executable, str(ORPHAN_SERVER), str(pid_path)),
        cwd=tmp_path,
        startup_timeout_seconds=3,
    )

    with pytest.raises(MCPError):
        client.start()

    child_pid = int(pid_path.read_text(encoding="utf-8"))
    try:
        deadline = time.monotonic() + 2
        while _process_running(child_pid) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert _process_running(child_pid) is False
    finally:
        if _process_running(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def _process_running(pid: int) -> bool:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        fields = stat_path.read_text(encoding="utf-8").split()
    except OSError:
        return False
    return len(fields) > 2 and fields[2] != "Z"


def test_mcp_runtime_exposes_scoped_tools_and_formats_untrusted_output(tmp_path):
    runtime = MCPRuntime([_server_config(tmp_path)]).start()
    try:
        assert runtime.status_summary() == [
            {"name": "echo", "status": "connected", "tool_count": 4, "error": ""}
        ]
        with scoped_dynamic_tools(runtime.tool_bindings()):
            assert get_execution_mode("mcp_echo_echo") == "read"
            assert get_execution_mode("mcp_echo_fail") == "write"
            assert is_transactional_write_tool("mcp_echo_fail") is False
            assert get_execution_mode("mcp_echo_delay") == "serial"
            output = dispatch("mcp_echo_echo", {"value": "hello"})
            assert '<mcp-output tool="mcp_echo_echo" untrusted="true">' in output
            assert "echo:hello" in output
            assert dispatch("mcp_echo_fail", {}).startswith("Error: MCP server 'echo'")
            assert '"answer": 42' in dispatch("mcp_echo_structured", {})
    finally:
        runtime.close()

    assert dispatch("mcp_echo_echo", {}) == "Error: Unknown tool 'mcp_echo_echo'"


def test_tool_executor_resolves_one_dynamic_provider_generation():
    """Permission and dispatch must use one immutable MCP binding snapshot."""
    provider_calls = []

    def provider():
        provider_calls.append(len(provider_calls) + 1)
        return [{
            "name": "mcp_snapshot_read",
            "description": "test",
            "parameters": {"type": "object", "properties": {}},
            "handler": lambda: "ok",
            "execution": "read",
            "side_effect": "reads-network",
        }]

    with scoped_dynamic_tool_provider(provider):
        provider_calls.clear()  # Ignore eager scope validation.
        result = ToolExecutor(PermissionManager("default")).execute_one({
            "id": "snapshot-call",
            "function": {"name": "mcp_snapshot_read", "arguments": "{}"},
        }, 0)

    assert result.executed is True
    assert result.output == "ok"
    assert provider_calls == [1]


def test_mcp_image_and_resource_blob_become_tool_attachments(tmp_path):
    import base64

    from nz_coder.tools import ToolOutput

    image = b"\x89PNG\r\n\x1a\nsmall"

    class MediaClient:
        def __init__(self, **kwargs):
            return None

        def start(self):
            return {}

        def list_tools(self):
            return [{"name": "media", "inputSchema": {"type": "object"}}]

        def close(self):
            return None

        def call_tool(self, tool_name, arguments):
            encoded = base64.b64encode(image).decode("ascii")
            return {
                "metadata": {"source": "fixture"},
                "content": [
                    {"type": "text", "text": "two images"},
                    {"type": "image", "mimeType": "image/png", "data": encoded},
                    {
                        "type": "resource",
                        "resource": {
                            "uri": "mcp://fixture/pixel.png",
                            "mimeType": "image/png",
                            "blob": encoded,
                        },
                    },
                ],
            }

    runtime = MCPRuntime(
        [MCPServerConfig(name="media", command=("unused",), cwd=tmp_path)],
        client_factory=MediaClient,
    ).start()
    try:
        with scoped_dynamic_tools(runtime.tool_bindings()):
            output = dispatch("mcp_media_media", {})
    finally:
        runtime.close()

    assert isinstance(output, ToolOutput)
    assert "two images" in output
    assert output.metadata == {"source": "fixture"}
    assert [item["mime"] for item in output.attachments] == [
        "image/png",
        "image/png",
    ]
    assert output.attachments[1]["filename"] == "pixel.png"


def test_mcp_runtime_isolates_server_startup_failures(tmp_path):
    missing = MCPServerConfig(
        name="missing",
        command=(str(tmp_path / "does-not-exist"),),
        cwd=tmp_path,
        startup_timeout_seconds=1,
        tool_timeout_seconds=1,
    )
    runtime = MCPRuntime([missing, _server_config(tmp_path, name="healthy")]).start()
    try:
        statuses = {item["name"]: item for item in runtime.status_summary()}
        assert statuses["missing"]["status"] == "failed"
        assert statuses["missing"]["error"] == "MCPError"
        assert statuses["healthy"]["status"] == "connected"
        assert len(runtime.tool_bindings()) == 4
    finally:
        runtime.close()


def test_mcp_optional_discovery_transport_failure_rejects_generation(tmp_path):
    class BrokenOptionalClient:
        def __init__(self, **_kwargs):
            self.closed = False

        def start(self):
            return {}

        def list_tools(self):
            return [{"name": "cached"}]

        def list_prompts(self):
            raise MCPError("transport died")

        def close(self):
            self.closed = True

    runtime = MCPRuntime(
        [MCPServerConfig(name="dead", command=("unused",), cwd=tmp_path)],
        client_factory=BrokenOptionalClient,
    ).start()
    try:
        assert runtime.statuses["dead"].status == "failed"
        assert runtime.statuses["dead"].error == "MCPError"
        assert runtime.tool_bindings() == []
        assert runtime.clients == {}
    finally:
        runtime.close()


def test_mcp_cache_refresh_failure_retires_stale_bindings(tmp_path):
    class RefreshClient:
        def __init__(self, **_kwargs):
            self.fail = False
            self.closed = False

        def start(self):
            return {}

        def list_tools(self):
            if self.fail:
                raise MCPError("transport died")
            return [{"name": "cached"}]

        def set_notification_handler(self, _method, _handler):
            return None

        def close(self):
            self.closed = True

    server = MCPServerConfig(name="refresh", command=("unused",), cwd=tmp_path)
    runtime = MCPRuntime([server], client_factory=RefreshClient).start()
    client = runtime.clients["refresh"]
    changes = []
    runtime.set_change_handler(lambda change, name: changes.append((change, name)))
    client.fail = True
    runtime._refresh_cache(server, client, "tools")
    try:
        assert runtime.statuses["refresh"].status == "failed"
        assert runtime.tool_bindings() == []
        assert runtime.clients == {}
        assert client.closed is True
        assert changes == [("failed", "refresh")]
    finally:
        runtime.close()


def test_mcp_runtime_refreshes_tools_prompts_and_resources_on_notifications(
    tmp_path,
):
    changes = []
    runtime = MCPRuntime([_server_config(tmp_path)]).start()
    runtime.set_change_handler(lambda change, server: changes.append((change, server)))
    try:
        assert [item["name"] for item in runtime.prompt_definitions()] == ["review"]
        assert [item["uri"] for item in runtime.resource_definitions()] == [
            "test://guide"
        ]
        assert runtime.get_prompt("echo", "review", {"topic": "runtime"})[
            "messages"
        ][0]["content"]["text"] == "Review runtime"
        assert runtime.read_resource("echo", "test://guide")["contents"][0][
            "text"
        ] == "guide-body"

        with scoped_dynamic_tool_provider(runtime.tool_bindings):
            assert "mcp_echo_fresh" not in {
                item["function"]["name"] for item in get_specs()
            }
            runtime.clients["echo"].request("test/change", {}, timeout=1)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                names = {item["name"] for item in runtime.tool_bindings()}
                prompt_names = {
                    item["name"] for item in runtime.prompt_definitions()
                }
                resource_uris = {
                    item["uri"] for item in runtime.resource_definitions()
                }
                if (
                    "mcp_echo_fresh" in names
                    and "fresh-prompt" in prompt_names
                    and "test://fresh" in resource_uris
                ):
                    break
                time.sleep(0.01)
            assert "mcp_echo_fresh" in {
                item["function"]["name"] for item in get_specs()
            }

        assert "mcp_echo_fresh" in names
        assert "fresh-prompt" in prompt_names
        assert "test://fresh" in resource_uris
        assert set(changes) == {
            ("tools_changed", "echo"),
            ("prompts_changed", "echo"),
            ("resources_changed", "echo"),
        }
    finally:
        runtime.close()


def test_mcp_runtime_connect_disconnect_reconnect(tmp_path):
    runtime = MCPRuntime([_server_config(tmp_path)]).start()
    first_client = runtime.clients["echo"]
    first_identities = {
        binding["name"]: binding["binding_identity"]
        for binding in runtime.tool_bindings()
    }
    try:
        assert runtime.disconnect("echo").status == "disabled"
        assert runtime.tool_bindings() == []
        assert first_client.process is not None
        assert first_client.process.poll() is not None

        assert runtime.connect("echo").status == "connected"
        second_client = runtime.clients["echo"]
        assert second_client is not first_client
        second_bindings = runtime.tool_bindings()
        second_identities = {
            binding["name"]: binding["binding_identity"]
            for binding in second_bindings
        }
        assert len(second_bindings) == 4
        assert second_identities.keys() == first_identities.keys()
        assert all(
            second_identities[name] != first_identities[name]
            for name in first_identities
        )

        assert runtime.reconnect("echo").status == "connected"
        assert runtime.clients["echo"] is not second_client
        third_identities = {
            binding["name"]: binding["binding_identity"]
            for binding in runtime.tool_bindings()
        }
        assert all(
            third_identities[name] != second_identities[name]
            for name in second_identities
        )
        with pytest.raises(MCPError, match="Unknown MCP server"):
            runtime.connect("missing")
    finally:
        runtime.close()


def test_mcp_runtime_starts_servers_in_parallel_and_supports_background_wait(
    tmp_path,
):
    startup_barrier = threading.Barrier(2)

    class SlowClient:
        def __init__(self, **_kwargs):
            self.closed = False

        def start(self):
            startup_barrier.wait(timeout=1)
            return {}

        def list_tools(self):
            return []

        def close(self):
            self.closed = True

    configs = [
        MCPServerConfig(name=name, command=("unused",), cwd=tmp_path)
        for name in ("one", "two")
    ]
    runtime = MCPRuntime(configs, client_factory=SlowClient)
    started_at = time.monotonic()
    runtime.start_background()
    returned_after = time.monotonic() - started_at
    try:
        assert returned_after < 0.1
        assert runtime.wait_ready(timeout=2) is True
        assert {item["status"] for item in runtime.status_summary()} == {
            "connected"
        }
    finally:
        runtime.close()


def test_mcp_runtime_concurrent_start_waits_for_same_generation(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def __init__(self, **_kwargs):
            self.closed = False

        def start(self):
            entered.set()
            release.wait(timeout=2)
            return {}

        def list_tools(self):
            return []

        def close(self):
            self.closed = True
            release.set()

    runtime = MCPRuntime(
        [MCPServerConfig(name="one", command=("unused",), cwd=tmp_path)],
        client_factory=BlockingClient,
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(runtime.start)
        assert entered.wait(timeout=1)
        second = pool.submit(runtime.start)
        time.sleep(0.05)
        assert second.done() is False
        release.set()
        assert first.result(timeout=1) is runtime
        assert second.result(timeout=1) is runtime
    try:
        assert runtime.wait_ready(timeout=0) is True
        assert runtime.statuses["one"].status == "connected"
    finally:
        runtime.close()


def test_mcp_disconnect_during_connect_watcher_suppresses_late_connected(tmp_path):
    watching = threading.Event()
    release = threading.Event()
    changes = []

    class WatchingClient:
        def __init__(self, **_kwargs):
            self.closed = False

        def start(self):
            return {}

        def list_tools(self):
            return []

        def set_notification_handler(self, _method, _handler):
            watching.set()
            release.wait(timeout=2)

        def close(self):
            self.closed = True
            release.set()

    runtime = MCPRuntime(
        [
            MCPServerConfig(
                name="race",
                command=("unused",),
                cwd=tmp_path,
                enabled=False,
            )
        ],
        client_factory=WatchingClient,
    ).start()
    runtime.set_change_handler(lambda change, server: changes.append((change, server)))
    with ThreadPoolExecutor(max_workers=1) as pool:
        connecting = pool.submit(runtime.connect, "race")
        assert watching.wait(timeout=1)
        assert runtime.disconnect("race").status == "disabled"
        release.set()
        assert connecting.result(timeout=1).status == "disabled"
    try:
        assert ("connected", "race") not in changes
        assert runtime.clients == {}
    finally:
        runtime.close()


def test_mcp_lifecycle_events_are_linearized_with_state_changes(tmp_path):
    handler_entered = threading.Event()
    release_handler = threading.Event()
    changes = []

    class Client:
        def __init__(self, **_kwargs):
            self.closed = False

        def start(self):
            return {}

        def list_tools(self):
            return []

        def close(self):
            self.closed = True

    runtime = MCPRuntime(
        [
            MCPServerConfig(
                name="ordered",
                command=("unused",),
                cwd=tmp_path,
                enabled=False,
            )
        ],
        client_factory=Client,
    ).start()

    def on_change(change, server):
        changes.append((change, server))
        if change == "connected":
            handler_entered.set()
            release_handler.wait(timeout=2)

    runtime.set_change_handler(on_change)
    with ThreadPoolExecutor(max_workers=2) as pool:
        connected = pool.submit(runtime.connect, "ordered")
        assert handler_entered.wait(timeout=1)
        disconnected = pool.submit(runtime.disconnect, "ordered")
        time.sleep(0.05)
        assert disconnected.done() is False
        release_handler.set()
        assert connected.result(timeout=1).status == "connected"
        assert disconnected.result(timeout=1).status == "disabled"
    try:
        assert changes == [
            ("connected", "ordered"),
            ("disconnected", "ordered"),
        ]
    finally:
        runtime.close()


def test_mcp_disconnect_wins_race_with_background_startup(tmp_path):
    started = threading.Event()
    release = threading.Event()

    class BlockingClient:
        def __init__(self, **_kwargs):
            self.closed = False

        def start(self):
            started.set()
            release.wait(timeout=2)
            if self.closed:
                raise MCPError("closed")
            return {}

        def list_tools(self):
            return []

        def close(self):
            self.closed = True
            release.set()

    runtime = MCPRuntime(
        [MCPServerConfig(name="slow", command=("unused",), cwd=tmp_path)],
        client_factory=BlockingClient,
    ).start_background()
    try:
        assert started.wait(timeout=1)
        assert runtime.disconnect("slow").status == "disabled"
        assert runtime.wait_ready(timeout=2) is True
        assert runtime.status_summary() == [{
            "name": "slow",
            "status": "disabled",
            "tool_count": 0,
            "error": "",
        }]
        assert runtime.clients == {}
        assert runtime.tool_bindings() == []
    finally:
        runtime.close()


def test_failed_server_does_not_reserve_dynamic_tool_names(tmp_path):
    class FakeClient:
        def __init__(self, *, name, **kwargs):
            self.name = name

        def start(self):
            return {}

        def list_tools(self):
            if self.name == "a":
                return [{"name": "b_c"}, {"name": "b_c"}]
            return [{"name": "c"}]

        def close(self):
            return None

        def call_tool(self, tool_name, arguments):
            return {"content": [{"type": "text", "text": "ok"}]}

    configs = [
        MCPServerConfig(name="a", command=("unused",), cwd=tmp_path),
        MCPServerConfig(name="a_b", command=("unused",), cwd=tmp_path),
    ]
    runtime = MCPRuntime(configs, client_factory=FakeClient).start()
    try:
        assert runtime.statuses["a"].status == "failed"
        assert runtime.statuses["a_b"].status == "connected"
        assert [binding["name"] for binding in runtime.tool_bindings()] == [
            "mcp_a_b_c"
        ]
    finally:
        runtime.close()


def test_long_mcp_tool_name_uses_stable_digest(tmp_path):
    long_name = "x" * 100

    class FakeClient:
        def __init__(self, **kwargs):
            return None

        def start(self):
            return {}

        def list_tools(self):
            return [{"name": long_name, "inputSchema": None}]

        def close(self):
            return None

        def call_tool(self, tool_name, arguments):
            return {"content": []}

    runtime = MCPRuntime(
        [MCPServerConfig(name="long", command=("unused",), cwd=tmp_path)],
        client_factory=FakeClient,
    ).start()
    try:
        binding = runtime.tool_bindings()[0]
        assert len(binding["name"]) == 64
        assert binding["name"].startswith("mcp_long_")
        assert binding["parameters"] == {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        }
    finally:
        runtime.close()


def test_mcp_runtime_close_interrupts_in_flight_startup(tmp_path):
    started = threading.Event()
    released = threading.Event()
    finished = threading.Event()
    instances = []

    class BlockingClient:
        def __init__(self, **kwargs):
            self.closed = False
            instances.append(self)

        def start(self):
            started.set()
            try:
                if not released.wait(timeout=2):
                    raise RuntimeError("test startup did not release")
                if self.closed:
                    raise RuntimeError("closed during startup")
                return {}
            finally:
                finished.set()

        def list_tools(self):
            return []

        def close(self):
            self.closed = True
            released.set()

    runtime = MCPRuntime(
        [MCPServerConfig(name="slow", command=("unused",), cwd=tmp_path)],
        client_factory=BlockingClient,
    )

    async def cancel_during_startup():
        task = asyncio.create_task(asyncio.to_thread(runtime.start))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await asyncio.to_thread(runtime.close)

    asyncio.run(cancel_during_startup())

    assert finished.wait(timeout=1)
    assert instances and instances[0].closed is True
    assert runtime.clients == {}
    assert runtime.tool_bindings() == []


def test_mcp_permissions_are_conservative_for_external_side_effects():
    definitions = [
        {"name": "mcp_demo_read", "handler": lambda: "ok", "execution": "read"},
        {"name": "mcp_demo_serial", "handler": lambda: "ok", "execution": "serial"},
        {"name": "mcp_demo_write", "handler": lambda: "ok", "execution": "write"},
    ]
    with scoped_dynamic_tools(definitions):
        assert _permission("default", "mcp_demo_read")["behavior"] == "allow"
        assert _permission("default", "mcp_demo_serial")["behavior"] == "ask"
        assert _permission("acceptEdits", "mcp_demo_write")["behavior"] == "ask"
        assert _permission("plan", "mcp_demo_read")["behavior"] == "allow"
        assert _permission("plan", "mcp_demo_serial")["behavior"] == "deny"
        assert _permission("auto", "mcp_demo_write")["behavior"] == "allow"
        allowed = PermissionChecker("default").check(
            "mcp_demo_write",
            {},
            [PermissionRule("mcp_demo_write", "allow")],
            [],
            [],
        )
        assert allowed["behavior"] == "allow"


def test_mcp_external_write_is_not_reported_as_local_transaction_write():
    definition = {
        "name": "mcp_demo_external_write",
        "handler": lambda **kwargs: "updated externally",
        "execution": "write",
        "transactional": False,
    }
    tool_call = {
        "function": {
            "name": "mcp_demo_external_write",
            "arguments": json.dumps({"value": 1}),
        }
    }
    with scoped_dynamic_tools([definition]):
        result = ToolExecutor(PermissionManager("auto")).execute_one(tool_call, 0)

    assert result.executed is True
    assert result.dispatch_failed is False
    assert result.is_write is False


def _permission(mode: str, name: str) -> dict:
    return PermissionChecker(mode).check(name, {}, [], [], [])


def test_dynamic_tool_overlays_are_thread_local():
    barrier = threading.Barrier(2)

    def worker(value: str) -> tuple[str, bool]:
        with scoped_dynamic_tools(
            [{"name": "mcp_same_tool", "handler": lambda: value, "execution": "read"}]
        ):
            barrier.wait(timeout=2)
            visible = any(
                spec["function"]["name"] == "mcp_same_tool" for spec in get_specs()
            )
            return dispatch("mcp_same_tool", {}), visible

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, ["workspace-a", "workspace-b"]))

    assert sorted(results) == [("workspace-a", True), ("workspace-b", True)]
    assert dispatch("mcp_same_tool", {}) == "Error: Unknown tool 'mcp_same_tool'"


def test_subagents_do_not_inherit_parent_mcp_tools():
    from nz_coder.runtime.agent.subagent import _subagent_tools

    with scoped_dynamic_tools(
        [{"name": "mcp_parent_private", "handler": lambda: "ok", "execution": "read"}]
    ):
        names = {spec["function"]["name"] for spec in _subagent_tools("general")}

    assert "mcp_parent_private" not in names


def test_child_scope_clears_and_restores_parent_dynamic_tool_overlay():
    definition = {
        "name": "mcp_parent_private",
        "handler": lambda: "parent",
        "execution": "read",
    }
    with scoped_dynamic_tools([definition]):
        assert dispatch("mcp_parent_private", {}) == "parent"
        with scoped_dynamic_tools_disabled():
            assert dispatch("mcp_parent_private", {}) == (
                "Error: Unknown tool 'mcp_parent_private'"
            )
            assert all(
                spec["function"]["name"] != "mcp_parent_private"
                for spec in get_specs()
            )
        assert dispatch("mcp_parent_private", {}) == "parent"


def test_agent_loop_reuses_mcp_runtime_until_agent_close(tmp_path, monkeypatch):
    from nz_coder.foundation import config
    from nz_coder.runtime.execution import loop as loop_module

    events: list[str] = []

    class FakeRuntime:
        def start(self):
            events.append("start")
            return self

        def tool_bindings(self):
            return [
                {
                    "name": "mcp_fake_ping",
                    "description": "fake",
                    "parameters": {"type": "object", "properties": {}},
                    "execution": "read",
                    "handler": lambda: "pong",
                }
            ]

        def status_summary(self):
            return [{"name": "fake", "status": "connected", "tool_count": 1, "error": ""}]

        def set_change_handler(self, handler):
            self.change_handler = handler

        def close(self):
            events.append("close")

    runtime = FakeRuntime()

    class RuntimeFactory:
        @staticmethod
        def configured(*, workspace=None, config_snapshot=None):
            assert workspace == tmp_path
            assert config_snapshot.workspace == tmp_path
            return runtime

    class Message:
        content = "done"
        tool_calls = []
        reasoning_content = None

    class Completions:
        def __init__(self):
            self.requests = []

        def create(self, **kwargs):
            self.requests.append(kwargs)
            return type("Response", (), {"choices": [type("Choice", (), {"message": Message()})()]})()

    completions = Completions()
    client = type(
        "Client",
        (),
        {"chat": type("Chat", (), {"completions": completions})()},
    )()
    monkeypatch.setattr(config, "WORKDIR", tmp_path)
    monkeypatch.setattr(loop_module, "MCPRuntime", RuntimeFactory)
    agent = loop_module.AgentLoop(
        "test",
        permission_mode="auto",
        client=client,
        trace_enabled=False,
    )

    result = asyncio.run(
        agent.run([{"role": "user", "content": "finish"}], stream=False)
    )
    second = asyncio.run(
        agent.run([{"role": "user", "content": "again"}], stream=False)
    )

    tool_names = {
        spec["function"]["name"] for spec in completions.requests[0]["tools"]
    }
    assert result["status"] == "completed"
    assert second["status"] == "completed"
    assert "mcp_fake_ping" in tool_names
    assert events == ["start", "start"]
    agent.close()
    assert events == ["start", "start", "close"]
    assert dispatch("mcp_fake_ping", {}) == "Error: Unknown tool 'mcp_fake_ping'"
