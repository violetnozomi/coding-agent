"""OFFLINE ONLY native CLI wiring probe; no task solutions or live credentials."""
from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

from .runner import BudgetExceeded, RequestBudget, write_json


def main(argv: list[str] | None = None) -> int:
    repo_arg, session_id, evidence_arg, limit_arg = argv or sys.argv[1:]
    repo, evidence = Path(repo_arg).resolve(), Path(evidence_arg).resolve()
    count_limit = int(limit_arg)
    budget = RequestBudget(count_limit * 10, Decimal(count_limit))
    record = {"evidence_kind": "offline/driver_validation", "provider_calls": 0,
              "budget_stopped": False, "network_attempts": 0,
              "profile": "main", "assembly": "declared-agent-graph",
              "permission_mode": "auto", "stream": False, "persist_session": True,
              "provider": "offline", "model": "offline-model", "effort": None,
              "system_instruction_source": "nz_coder.runtime.conversation.prompt.build",
              "tool_schemas_by_request": [], "request_facts": []}

    def forbidden_network(*_args, **_kwargs):
        record["network_attempts"] += 1
        write_json(evidence, record)
        raise AssertionError("Offline wiring must not connect to a network")

    # Fail before any accidental SDK transport. No user credentials are inherited.
    socket.socket.connect = forbidden_network
    socket.socket.connect_ex = forbidden_network
    socket.create_connection = forbidden_network
    socket.getaddrinfo = forbidden_network

    from nz_coder.interface.cli import main as product_main
    from nz_coder.runtime.execution import native_sdk
    from nz_coder.runtime.execution.loop import AgentLoop
    from nz_coder.providers.capabilities import ModelCapabilities
    from nz_coder.runtime.model_gateway import ResolvedModelRuntime
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot

    # These are organizer-created trusted fixtures. Grant the exact captured
    # control fingerprint through the normal store, confined to this fresh HOME.
    snapshot = load_config_snapshot(repo)
    WorkspaceTrustStore().trust(repo, "workspace-control", snapshot.control_fingerprint)
    record["workspace_trust"] = "exact fixture fingerprint in isolated user store"

    class Provider:
        name = "offline"

        def create_completion(self, _client, **kwargs):
            try:
                budget.reserve(10, Decimal(1))  # Fake credits, NEVER a live price estimate.
            except BudgetExceeded:
                record["budget_stopped"] = True
                write_json(evidence, record)
                raise
            record["provider_calls"] += 1
            calls = record["provider_calls"]
            tools = sorted(t.get("function", {}).get("name", "") for t in kwargs.get("tools", []))
            record["tool_schemas_by_request"].append(tools)
            system = "\n".join(str(m.get("content", "")) for m in kwargs.get("messages", [])
                               if m.get("role") == "system")
            record["request_facts"].append({
                "model": kwargs.get("model"), "max_tokens": kwargs.get("max_tokens"),
                "system_sha256_workspace_normalized": hashlib.sha256(
                    system.replace(str(repo), "<WORKSPACE>").encode()).hexdigest(),
                "message_roles": [m.get("role") for m in kwargs.get("messages", [])],
            })
            sequence = [
                ("list_directory", {"path": ".", "depth": 1}),
                ("write_file", {"path": "tests/driver_probe.txt", "content": "OFFLINE wiring only; not a task solution.\n"}),
                ("bash", {"command": "python -m pytest -q tests"}),
            ]
            if calls <= len(sequence):
                name, arguments = sequence[calls - 1]
                message = SimpleNamespace(content="", tool_calls=[SimpleNamespace(
                    id=f"offline-{calls}", type="function", function=SimpleNamespace(
                        name=name, arguments=json.dumps(arguments)))])
                finish = "tool_calls"
            else:
                message = SimpleNamespace(content="Offline wiring probe finished; no coding task solved.", tool_calls=[])
                finish = "stop"
            write_json(evidence, record)
            return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)],
                                   usage=SimpleNamespace(prompt_tokens=2, completion_tokens=1, total_tokens=3))

    def runtime(_request):
        return ResolvedModelRuntime(
            provider_id="offline", model_id="offline-model", request_model_id="offline-model",
            variant=None, provider=provider, client=object(), owns_client=False,
            capabilities=ModelCapabilities(provider="offline", model_id="offline-model", supports_streaming=False),
        )

    def no_legacy(*_args, **_kwargs):
        raise AssertionError("Native headless baseline must not instantiate legacy AgentLoop")

    original_builder = native_sdk.build_product_run_environment

    def observe_environment(*args, **kwargs):
        environment = original_builder(*args, **kwargs)
        record["environment_class"] = type(environment).__name__
        record["runner_class"] = type(environment.runner).__name__
        record["services"] = {key: type(getattr(environment.runtime_services, key, None)).__name__
                              for key in ("model", "tools", "context", "memory", "verifier", "session_runtime")}
        write_json(evidence, record)
        return environment

    provider = Provider()
    native_sdk.resolve_model_runtime = runtime
    native_sdk.build_product_run_environment = observe_environment
    AgentLoop.__init__ = no_legacy
    write_json(evidence, record)
    try:
        return product_main([
            "run", "--cwd", str(repo), "--provider", "offline", "--model", "offline-model",
            "--permission-mode", "auto", "--session", session_id, "--max-turns", "6",
            "--output", "jsonl", "--prompt",
            "Offline driver validation only: inspect the repository, create tests/driver_probe.txt "
            "with the offline marker, run python -m pytest -q tests, and report. Do not fix any source files.",
        ])
    finally:
        # Only record relative containment, never private host locations or session contents.
        record["session_files_in_isolated_home"] = len(list(Path.home().rglob(f"{session_id}.json")))
        from nz_coder.state.workdir import scoped_workdir, current_derived_path
        with scoped_workdir(repo):
            paths = [current_derived_path(name).resolve() for name in
                     ("SESSION_DIR", "MEMORY_DIR", "TRACE_DIR", "TOOL_RESULTS_DIR", "INDEX_CACHE_DIR")]
        record["private_state_in_isolated_home"] = all(Path.home() in path.parents for path in paths)
        record["workspace_local_state_directories"] = sorted(
            p.name for p in (repo / ".nz-coder").iterdir()
        ) if (repo / ".nz-coder").is_dir() else []
        record["reserved_fake_tokens"] = budget.reserved_tokens
        record["reserved_fake_credits"] = str(budget.reserved_cost)
        write_json(evidence, record)


if __name__ == "__main__":
    raise SystemExit(main())
