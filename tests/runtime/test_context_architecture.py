"""Architecture guards for context and session runtime extraction."""
from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
        elif isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
    return result


def test_runtime_core_has_no_coding_or_legacy_concrete_imports() -> None:
    forbidden = (
        "nz_coder.runtime.execution.loop", "nz_coder.interface", "nz_coder.tools",
        "nz_coder.lsp", "nz_coder.runtime.verification", "nz_coder.greenfield",
        "nz_coder.repo",
    )
    for path in (ROOT / "nz_coder" / "runtime" / "core").glob("*.py"):
        imports = _imports(path)
        assert not any(
            module == prefix or module.startswith(prefix + ".")
            for module in imports for prefix in forbidden
        ), f"{path.name} imports a coding/legacy concrete: {sorted(imports)}"


def test_background_and_workflow_do_not_own_model_or_tool_loops() -> None:
    manager = (ROOT / "nz_coder/runtime/agent/agent_manager.py").read_text(encoding="utf-8")
    workflow = (ROOT / "nz_coder/runtime/workflows/workflow_runtime.py").read_text(encoding="utf-8")
    assert "run_subagent(" in manager
    assert "BackgroundAgentManager" in workflow
    for source in (manager, workflow):
        assert ".complete_turn(" not in source
        assert ".execute_batch_async(" not in source
        assert "for turn_index" not in source


def test_capability_modules_do_not_depend_on_agent_loop() -> None:
    capability_paths = (
        "nz_coder/tool_platform/catalog.py",
        "nz_coder/tool_platform/search.py",
        "nz_coder/tool_platform/exposure.py",
        "nz_coder/intelligence/repository_graph.py",
        "nz_coder/tools/repo_context.py",
        "nz_coder/tools/tool_search.py",
        "nz_coder/state/skills.py",
        "nz_coder/tool_platform/results.py",
        "nz_coder/state/memory_control.py",
        "nz_coder/tools/mcp_catalog.py",
    )
    for relative in capability_paths:
        imports = _imports(ROOT / relative)
        assert "nz_coder.runtime.execution.loop" not in imports
        assert "nz_coder.loop" not in imports


def test_production_host_binds_mcp_catalog_to_run_scope() -> None:
    source = (ROOT / "nz_coder/runtime/execution/host.py").read_text(encoding="utf-8")
    assert "scoped_mcp_runtime(mcp_runtime)" in source
    assert source.count("scoped_mcp_runtime(mcp_runtime)") == 1


def test_loop_context_entrypoints_are_compatibility_facades() -> None:
    path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("_compact_if_needed", "_compact_if_needed_async"):
        assert "ProductionContextManager" in methods[name] or "context_manager" in methods[name]
        assert "micro_compact" not in methods[name]
        assert "persist_oversized_user_inputs" not in methods[name]

    checkpoint = methods["_checkpoint_messages"]
    assert "FileSessionRepository" in checkpoint
    assert "save_session(" not in checkpoint


def test_production_service_graph_has_one_session_owner() -> None:
    contracts = (
        ROOT / "nz_coder" / "runtime" / "core" / "contracts.py"
    ).read_text(encoding="utf-8")
    services = (ROOT / "nz_coder" / "runtime" / "execution" / "services.py").read_text(
        encoding="utf-8"
    )
    loop = (ROOT / "nz_coder" / "runtime" / "execution" / "loop.py").read_text(
        encoding="utf-8"
    )

    runtime_services = contracts.split("class RuntimeServices:", 1)[1]
    assert "sessions:" not in runtime_services
    assert "sessions=" not in services
    assert "runtime_services.sessions" not in loop


def test_context_and_session_adapters_do_not_import_agent_loop() -> None:
    for relative in (
        "nz_coder/runtime/conversation/context_manager.py",
        "nz_coder/runtime/session/session_repository.py",
        "nz_coder/runtime/execution/runner.py",
    ):
        path = ROOT / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        assert not any(
            isinstance(node, ast.ImportFrom) and node.module == "nz_coder.runtime.execution.loop"
            for node in ast.walk(tree)
        )


def test_context_manager_consumes_focused_context_not_agent_host() -> None:
    """Context budgeting must not regain direct AgentLoop state access."""
    path = ROOT / "nz_coder" / "runtime" / "conversation" / "context_manager.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    manager = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProductionContextManager"
    )
    implementation = ast.get_source_segment(source, manager) or ""

    assert "ContextExecutionContext" in implementation
    assert "host." not in implementation
    assert "context.workspace" in implementation
    assert "context.projected_tokens" in implementation
    assert "context.compact" in implementation


def test_runner_injects_focused_context_into_every_tool_batch() -> None:
    """A new Runner tool path must not bypass run-scoped Tool ownership."""
    path = ROOT / "nz_coder" / "runtime" / "execution" / "runner.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute_batch_async"
    ]

    assert calls
    for call in calls:
        first_argument = ast.get_source_segment(source, call.args[0]) or ""
        assert "resolve_tool_runtime_context" in first_argument

    pipeline_path = ROOT / "nz_coder" / "runtime" / "tool_runtime" / "pipeline.py"
    pipeline_source = pipeline_path.read_text(encoding="utf-8")
    pipeline_tree = ast.parse(pipeline_source, filename=str(pipeline_path))
    execute_async = next(
        node
        for node in ast.walk(pipeline_tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_execute_batch_async_snapshot"
    )
    async_source = ast.get_source_segment(pipeline_source, execute_async) or ""
    assert "lifecycle.checkpoint" in async_source
    public_execute_async = next(
        node
        for node in ast.walk(pipeline_tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "execute_batch_async"
    )
    public_async_source = (
        ast.get_source_segment(pipeline_source, public_execute_async) or ""
    )
    assert "scoped_dynamic_tool_snapshot" in public_async_source
    assert "_execute_batch_async_snapshot" in public_async_source
    assert "host._checkpoint_messages" not in async_source


def test_async_tool_runtime_is_host_free_but_sync_compatibility_is_explicit() -> None:
    """Production Tool services must not regain AgentLoop private access."""
    pipeline_path = ROOT / "nz_coder" / "runtime" / "tool_runtime" / "pipeline.py"
    pipeline_source = pipeline_path.read_text(encoding="utf-8")
    pipeline_tree = ast.parse(pipeline_source, filename=str(pipeline_path))
    for name in (
        "execute_batch_async",
        "_execute_batch_async_snapshot",
        "dispatch_async",
    ):
        method = next(
            node
            for node in ast.walk(pipeline_tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == name
        )
        implementation = ast.get_source_segment(pipeline_source, method) or ""
        assert "host." not in implementation

    for relative, class_name in (
        ("nz_coder/runtime/tool_runtime/policy.py", "ProductionToolPolicy"),
        (
            "nz_coder/runtime/tool_runtime/result_projection.py",
            "ProductionToolResultProjector",
        ),
    ):
        path = ROOT / relative
        module_source = path.read_text(encoding="utf-8")
        module_tree = ast.parse(module_source, filename=str(path))
        service = next(
            node
            for node in module_tree.body
            if isinstance(node, ast.ClassDef) and node.name == class_name
        )
        implementation = ast.get_source_segment(module_source, service) or ""
        assert "host." not in implementation

    sync_method = next(
        node
        for node in ast.walk(pipeline_tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_execute_batch_sync_snapshot"
    )
    sync_source = ast.get_source_segment(pipeline_source, sync_method) or ""
    assert "host." in sync_source


def test_main_loop_is_owned_by_shared_runner() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    loop_source = loop_path.read_text(encoding="utf-8")
    loop_tree = ast.parse(loop_source, filename=str(loop_path))
    loop_run = next(
        node
        for node in ast.walk(loop_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run"
    )
    facade = ast.get_source_segment(loop_source, loop_run) or ""
    assert "_run_native_facade" in facade
    assert "for turn_index" not in facade

    runner_path = ROOT / "nz_coder" / "runtime" / "execution" / "runner.py"
    runner_source = runner_path.read_text(encoding="utf-8")
    runner_tree = ast.parse(runner_source, filename=str(runner_path))
    host_run = next(
        node
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_turns"
    )
    implementation = ast.get_source_segment(runner_source, host_run) or ""
    assert "for turn_index" in implementation
    assert "services.model.complete_turn" in implementation
    assert "services.tools.execute_batch_async" in implementation

    public_run = next(
        node
        for node in ast.walk(loop_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "run"
    )
    public_facade = ast.get_source_segment(loop_source, public_run) or ""
    assert "_run_native_facade" in public_facade
    assert "ExitStack" not in public_facade
    assert "MCPRuntime" not in public_facade


def test_agent_runner_exposes_only_one_execution_state_machine() -> None:
    path = ROOT / "nz_coder" / "runtime" / "execution" / "runner.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    runner = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AgentRunner"
    )
    async_methods = {
        node.name: node
        for node in runner.body
        if isinstance(node, ast.AsyncFunctionDef)
    }
    assert "run" in async_methods
    assert "run_host" not in async_methods
    loop_owners = [
        name
        for name, method in async_methods.items()
        if any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(method))
    ]
    assert loop_owners == ["_run_turns"]

    implementation = ast.get_source_segment(source, async_methods["_run_turns"]) or ""
    for required in (
        "context.lifecycle.initialize",
        "context.lifecycle.finalize",
        "context.policy.run_input_guardrails",
        "context.policy.prepare_user_images",
        "context.policy.prepare_user_documents",
        "context.policy.resolve_structured_output",
        "context.policy.return_from_as_tool",
        "services.context.prepare_async",
        "services.model.complete_turn",
        "services.tools.execute_batch_async",
        "services.session_runtime.checkpoint",
        "self._settle_terminal_boundary",
    ):
        assert required in implementation
    settlement = ast.get_source_segment(
        source,
        async_methods["_settle_terminal_boundary"],
    ) or ""
    assert "context.policy.verify_completion" in settlement
    for forbidden in (
        "host._init_run",
        "host._finalize_async",
        "host._run_input_guardrails",
        "host._run_output_guardrails",
        "host._prepare_user_image_descriptions",
        "host._prepare_user_documents",
        "host._resolve_structured_agent_output",
        "host._return_from_as_tool",
        "host._compact_if_needed_async",
        "host._call_llm_async",
        "host._execute_tools_async",
        "host._checkpoint_messages",
        "host._check_verification_gate_async",
    ):
        assert forbidden not in implementation


def test_provider_projection_supersedes_stale_file_read_without_mutating_session():
    from nz_coder.runtime.conversation.message_projection import project_provider_messages

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "read-1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }]},
        {
            "role": "tool",
            "tool_call_id": "read-1",
            "content": "OLD CONTENT",
            "_nz_evidence_kind": "file_read",
            "_nz_resource": "src/app.py",
            "_nz_mutation_generation": 0,
        },
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "write-1",
            "type": "function",
            "function": {"name": "edit_file", "arguments": "{}"},
        }]},
        {
            "role": "tool",
            "tool_call_id": "write-1",
            "content": "Updated src/app.py",
            "_nz_evidence_kind": "file_write",
            "_nz_mutated_resources": ["src/app.py"],
            "_nz_mutation_generation": 1,
        },
    ]

    projected = project_provider_messages(messages)

    assert messages[1]["content"] == "OLD CONTENT"
    assert projected[1]["content"] == (
        "[Earlier read of src/app.py omitted: file changed in mutation generation 1.]"
    )
    assert projected[3]["content"] == "Updated src/app.py"


def test_provider_projection_supersedes_failed_verification_after_later_pass():
    from nz_coder.runtime.conversation.message_projection import project_provider_messages

    messages = [
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "test-1", "type": "function",
            "function": {"name": "bash", "arguments": "{}"},
        }]},
        {
            "role": "tool",
            "tool_call_id": "test-1",
            "content": "TRACEBACK\n1 failed",
            "_nz_evidence_kind": "verification",
            "_nz_resource": "targeted",
            "_nz_mutation_generation": 1,
            "_nz_verification_passed": False,
        },
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "test-2", "type": "function",
            "function": {"name": "bash", "arguments": "{}"},
        }]},
        {
            "role": "tool",
            "tool_call_id": "test-2",
            "content": "94 passed",
            "_nz_evidence_kind": "verification",
            "_nz_resource": "targeted",
            "_nz_mutation_generation": 2,
            "_nz_verification_passed": True,
        },
    ]

    projected = project_provider_messages(messages)

    assert messages[1]["content"].startswith("TRACEBACK")
    assert projected[1]["content"] == (
        "[Earlier targeted verification failure from generation 1 omitted; "
        "superseded by a passing generation 2 result.]"
    )
    assert projected[3]["content"] == "94 passed"


def test_guardrail_policy_is_owned_by_runtime_service() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    loop_source = loop_path.read_text(encoding="utf-8")
    loop_tree = ast.parse(loop_source, filename=str(loop_path))
    methods = {
        node.name: ast.get_source_segment(loop_source, node) or ""
        for node in ast.walk(loop_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name, operation in (
        ("_run_input_guardrails", "run_input"),
        ("_run_output_guardrails", "run_output"),
        ("_run_tool_before_guardrails", "before_tool"),
        ("_run_tool_after_guardrails", "after_tool"),
    ):
        assert f"runtime_services.guardrails.{operation}" in methods[name]
        assert "validate_verdict" not in methods[name]

    service_path = ROOT / "nz_coder" / "runtime" / "agent" / "guardrail_runtime.py"
    service_source = service_path.read_text(encoding="utf-8")
    assert "class ProductionGuardrailRuntime" in service_source
    assert "nz_coder.runtime.execution.loop" not in service_source

    runner_source = (ROOT / "nz_coder" / "runtime" / "execution" / "runner.py").read_text(
        encoding="utf-8"
    )
    adapter_source = (
        ROOT / "nz_coder" / "runtime" / "adapters" / "runner.py"
    ).read_text(encoding="utf-8")
    commit_source = (
        ROOT / "nz_coder" / "runtime" / "execution" / "commit_boundary.py"
    ).read_text(encoding="utf-8")
    assert "context.policy.run_input_guardrails" in runner_source
    assert "approve_model_result" in runner_source
    assert "context.policy.run_output_guardrail" in commit_source
    assert "services.guardrails.run_input" in adapter_source
    assert "services.guardrails.run_output" in adapter_source
    assert "host._run_input_guardrails" not in runner_source
    assert "host._run_output_guardrails" not in runner_source
    assert "host._run_guardrails" not in runner_source


def test_attachment_preflight_is_owned_by_runtime_service() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    source = loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(loop_path))
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name, operation in (
        ("_prepare_user_image_descriptions", "prepare_user_images"),
        ("_prepare_user_documents", "prepare_user_documents"),
        ("_describe_read_tool_results_async", "describe_read_results"),
    ):
        assert f"runtime_services.inputs.{operation}" in methods[name]
        assert "describe_images(" not in methods[name]

    preflight_source = (
        ROOT / "nz_coder" / "runtime" / "conversation" / "input_preflight.py"
    ).read_text(encoding="utf-8")
    assert "class ProductionInputPreflight" in preflight_source
    assert "nz_coder.runtime.execution.loop" not in preflight_source

    runner_source = (ROOT / "nz_coder" / "runtime" / "execution" / "runner.py").read_text(
        encoding="utf-8"
    )
    adapter_source = (
        ROOT / "nz_coder" / "runtime" / "adapters" / "runner.py"
    ).read_text(encoding="utf-8")
    assert "context.policy.prepare_user_images" in runner_source
    assert "context.policy.prepare_user_documents" in runner_source
    assert "services.inputs.prepare_user_images" in adapter_source
    assert "services.inputs.prepare_user_documents" in adapter_source


def test_agent_transitions_are_owned_by_runtime_service() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    source = loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(loop_path))
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name, operation in (
        ("_resolve_structured_agent_output", "resolve_structured_output"),
        ("_tool_handoff_signal", "signal_from_metadata"),
        ("_apply_handoff_signal", "apply"),
        ("_return_from_as_tool", "return_from_as_tool"),
        ("_terminal_content", "terminal_content"),
    ):
        assert f"runtime_services.transitions.{operation}" in methods[name]

    transition_source = (
        ROOT / "nz_coder" / "runtime" / "agent" / "agent_transition_runtime.py"
    ).read_text(encoding="utf-8")
    assert "class ProductionAgentTransitionRuntime" in transition_source
    assert "nz_coder.runtime.execution.loop" not in transition_source

    runner_source = (ROOT / "nz_coder" / "runtime" / "execution" / "runner.py").read_text(
        encoding="utf-8"
    )
    adapter_source = (
        ROOT / "nz_coder" / "runtime" / "adapters" / "runner.py"
    ).read_text(encoding="utf-8")
    assert "context.policy.resolve_structured_output" in runner_source
    assert "context.policy.return_from_as_tool" in runner_source
    assert "services.transitions.resolve_structured_output" in adapter_source
    assert "services.transitions.return_from_as_tool" in adapter_source
    assert "host._resolve_structured_agent_output" not in runner_source
    assert "host._return_from_as_tool" not in runner_source


def test_tool_admission_and_scheduling_policy_is_owned_by_tool_runtime() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    source = loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(loop_path))
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    names = (
        "_tool_batch_has_write",
        "_tool_call_can_run_concurrently",
        "_agent_tool_rejections",
        "_admission_tool_rejections",
        "_strict_progress_rejections",
        "_begin_tool_batch",
        "_finish_tool_batch_observation",
        "_trace_tool_streak_reset",
        "_find_repeated_tool_calls",
        "_resolve_doom_loop_permissions",
        "_resolve_doom_loop_permissions_async",
    )
    for name in names:
        assert "tool_runtime" in methods[name] or "ProductionToolRuntime" in methods[name]
        assert len(methods[name].splitlines()) < 24

    policy_source = (
        ROOT / "nz_coder" / "runtime" / "tool_runtime" / "policy.py"
    ).read_text(encoding="utf-8")
    assert "class ProductionToolPolicy" in policy_source
    assert "nz_coder.runtime.execution.loop" not in policy_source

    pipeline_source = (
        ROOT / "nz_coder" / "runtime" / "tool_runtime" / "pipeline.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "host._find_repeated_tool_calls",
        "host._agent_tool_rejections",
        "host._admission_tool_rejections",
        "host._strict_progress_rejections",
        "host._finish_tool_batch_observation",
        "host._consume_dispatched_tools",
    ):
        assert forbidden not in pipeline_source

    projection_source = (
        ROOT / "nz_coder" / "runtime" / "tool_runtime" / "result_projection.py"
    ).read_text(encoding="utf-8")
    assert "class ProductionToolResultProjector" in projection_source
    assert "nz_coder.runtime.execution.loop" not in projection_source
    assert "tool_runtime" in methods["_consume_dispatched_tools"]
    assert len(methods["_consume_dispatched_tools"].splitlines()) < 18


def test_run_initialization_is_owned_by_lifecycle_service() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    source = loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(loop_path))
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_init_run"
    )
    facade = ast.get_source_segment(source, method) or ""
    assert "runtime_services.lifecycle.initialize" in facade
    assert len(facade.splitlines()) < 15
    assert "runtime_state.reset" not in facade

    lifecycle_source = (
        ROOT / "nz_coder" / "runtime" / "execution" / "run_lifecycle.py"
    ).read_text(encoding="utf-8")
    assert "class ProductionRunLifecycle" in lifecycle_source
    assert "nz_coder.runtime.execution.loop" not in lifecycle_source

    for name in ("_finalize", "_finalize_async"):
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        )
        facade = ast.get_source_segment(source, method) or ""
        assert "runtime_services.lifecycle" in facade
        assert "run_end" not in facade


def test_composition_installs_production_runtime_services() -> None:
    path = ROOT / "nz_coder" / "runtime" / "execution" / "composition.py"
    source = path.read_text(encoding="utf-8")
    assert "build_runtime_services" in source
    assert "runtime_services" in source


def test_model_turn_execution_is_owned_by_model_service() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    loop_source = loop_path.read_text(encoding="utf-8")
    loop_tree = ast.parse(loop_source, filename=str(loop_path))
    facade = next(
        node for node in ast.walk(loop_tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_call_llm_async"
    )
    facade_source = ast.get_source_segment(loop_source, facade) or ""
    assert "runtime_services.model.complete_turn" in facade_source
    assert "run_in_executor" not in facade_source
    assert "asyncio.shield" not in facade_source

    service_path = ROOT / "nz_coder" / "runtime" / "execution" / "services.py"
    service_source = service_path.read_text(encoding="utf-8")
    assert "run_in_executor" in service_source
    assert "asyncio.shield" in service_source
    assert "context.retire_message_part" in service_source


def test_memory_recall_and_terminal_learning_are_owned_by_memory_service() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    loop_source = loop_path.read_text(encoding="utf-8")
    loop_tree = ast.parse(loop_source, filename=str(loop_path))
    methods = {
        node.name: ast.get_source_segment(loop_source, node) or ""
        for node in ast.walk(loop_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "runtime_services.memory.prompt_block" in methods["_memory_block"]
    assert "build_prompt_block" not in methods["_memory_block"]
    assert "runtime_services.memory.finalize" in methods["_maybe_save_learnings_async"]
    assert "run_auto_memory_pipeline_async" not in methods["_maybe_save_learnings_async"]

    service_source = (ROOT / "nz_coder" / "runtime" / "execution" / "services.py").read_text(
        encoding="utf-8"
    )
    assert "ProductionMemoryService" in service_source
    assert "run_auto_memory_pipeline_async" in service_source
    assert "memory=ProductionMemoryService()" in service_source


def test_provider_message_projection_is_not_owned_by_agent_loop() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    loop_source = loop_path.read_text(encoding="utf-8")
    loop_tree = ast.parse(loop_source, filename=str(loop_path))
    method = next(
        node for node in ast.walk(loop_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_sanitize_messages"
    )
    facade = ast.get_source_segment(loop_source, method) or ""
    assert "project_provider_messages" in facade
    assert len(facade.splitlines()) < 20
    assert "normalize_attachments" not in facade

    projection_path = ROOT / "nz_coder" / "runtime" / "conversation" / "message_projection.py"
    source = projection_path.read_text(encoding="utf-8")
    assert "def project_provider_messages" in source
    assert "nz_coder.runtime.execution.loop" not in source


def test_agent_role_activation_is_owned_by_role_runtime() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    source = loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(loop_path))
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "role_runtime.activate" in methods["_activate_agent_runtime"]
    assert "resolve_model_runtime" not in methods["_activate_agent_runtime"]
    assert "role_runtime.escalate" in methods["_escalate_agent_reasoning"]

    role_path = ROOT / "nz_coder" / "runtime" / "agent" / "agent_role_runtime.py"
    role_source = role_path.read_text(encoding="utf-8")
    assert "class ProductionAgentRoleRuntime" in role_source
    assert "nz_coder.runtime.execution.loop" not in role_source


def test_buffered_provider_calls_are_owned_by_model_service() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    source = loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(loop_path))
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in ("_call_non_streaming_once", "_call_text_completion_once", "_call_non_streaming"):
        assert "runtime_services.model" in methods[name]
        assert "ModelCall(" not in methods[name]

    service_source = (ROOT / "nz_coder" / "runtime" / "execution" / "services.py").read_text(
        encoding="utf-8"
    )
    assert "def complete_buffered" in service_source
    assert "def complete_text" in service_source


def test_production_turn_model_runtime_consumes_focused_context() -> None:
    """The production model port must not regain broad AgentLoop access."""
    path = ROOT / "nz_coder" / "runtime" / "execution" / "services.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    runtime = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "ProductionTurnModelRuntime"
    )
    segment = ast.get_source_segment(source, runtime) or ""

    assert "host." not in segment
    assert "ModelExecutionContext" in segment

    runner_source = (
        ROOT / "nz_coder" / "runtime" / "execution" / "runner.py"
    ).read_text(encoding="utf-8")
    adapter_source = (
        ROOT / "nz_coder" / "runtime" / "adapters" / "runner.py"
    ).read_text(encoding="utf-8")
    assert "model_context_from_legacy_host" in adapter_source
    assert "resolve_model_runtime_context()" in runner_source


def test_memory_and_verifier_services_consume_focused_contexts() -> None:
    path = ROOT / "nz_coder/runtime/execution/services.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    classes = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.ClassDef)
    }
    for name in ("ProductionMemoryService", "ProductionCompletionVerifier"):
        segment = classes[name]
        assert "host." not in segment
        assert "vars(host)" not in segment
    assert "memory_context_from_legacy_host" in (
        ROOT / "nz_coder/runtime/execution/loop.py"
    ).read_text(encoding="utf-8")
    assert "verification_context_from_legacy_host" in (
        ROOT / "nz_coder/runtime/adapters/runner.py"
    ).read_text(encoding="utf-8")


def test_runner_turn_state_machine_consumes_focused_context() -> None:
    """The one production turn loop must not receive the compatibility host."""
    path = ROOT / "nz_coder" / "runtime" / "execution" / "runner.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_turns"
    )
    segment = ast.get_source_segment(source, method) or ""

    assert "host." not in segment
    assert "RunnerExecutionContext" in segment
    assert "runner_context_from_legacy_host" in source


def test_native_runner_path_cannot_reach_legacy_adapter_or_host() -> None:
    """Legacy conversion stays inside the explicitly named compatibility path."""
    path = ROOT / "nz_coder" / "runtime" / "execution" / "runner.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    methods = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    native = methods["_run_request"]
    assert "legacy" not in native.lower()
    assert "host" not in native.lower()
    assert "runner_context_from_legacy_host" not in native
    assert "run_request_from_legacy_host" not in native

    top_level_imports = [
        node
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert not any(
        isinstance(node, ast.ImportFrom)
        and str(node.module or "").startswith("nz_coder.runtime.adapters")
        for node in top_level_imports
    )
    assert "from nz_coder.runtime.adapters.runner import" in methods["_run_legacy"]


def test_stream_projection_and_result_type_are_outside_agent_loop() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    source = loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(loop_path))
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_call_streaming_gateway"
    )
    facade = ast.get_source_segment(source, method) or ""
    assert "project_streaming_turn" in facade
    assert len(facade.splitlines()) < 20
    assert not any(
        isinstance(node, ast.ClassDef) and node.name == "LLMResult"
        for node in tree.body
    )

    stream_source = (ROOT / "nz_coder" / "runtime" / "execution" / "provider_stream.py").read_text(
        encoding="utf-8"
    )
    assert "def project_streaming_turn" in stream_source
    assert "nz_coder.runtime.execution.loop" not in stream_source


def test_prompt_layer_assembly_is_owned_by_context_runtime() -> None:
    loop_path = ROOT / "nz_coder" / "runtime" / "execution" / "loop.py"
    source = loop_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(loop_path))
    method = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_build_api_messages"
    )
    facade = ast.get_source_segment(source, method) or ""
    assert "prompt_builder.build" in facade
    assert len(facade.splitlines()) < 15
    assert "load_instruction_context" not in facade
    assert "_build_context_layers" not in facade

    prompt_source = (ROOT / "nz_coder" / "runtime" / "conversation" / "prompt_builder.py").read_text(
        encoding="utf-8"
    )
    assert "class ProductionPromptBuilder" in prompt_source
    assert "nz_coder.runtime.execution.loop" not in prompt_source


def test_child_facade_has_no_provider_or_tool_turn_loop() -> None:
    path = ROOT / "nz_coder" / "runtime" / "agent" / "subagent.py"
    source = path.read_text(encoding="utf-8")
    assert "AgentLoop(" not in source
    assert "declared_runtime(graph).build" in source
    tree = ast.parse(source, filename=str(path))
    child = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "run_subagent"
    )
    implementation = ast.get_source_segment(source, child) or ""
    assert "declared_runtime(graph).build" in implementation
    assert "agent.run(" in implementation
    assert "agent.runner.run(" not in implementation
    assert "for turn_index" not in implementation
    assert "ModelCall(" not in implementation
    assert "ProductionModelGateway(" not in implementation


def test_dependency_direction_has_no_state_to_runtime_or_core_to_interface_edge() -> None:
    failures = []
    scopes = (
        (ROOT / "nz_coder" / "state", "nz_coder.runtime"),
        (ROOT / "nz_coder" / "runtime" / "core", "nz_coder.interface"),
    )
    for scope, forbidden_prefix in scopes:
        for path in scope.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and str(node.module or "").startswith(forbidden_prefix):
                    failures.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.module}")
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(forbidden_prefix):
                            failures.append(f"{path.relative_to(ROOT)}:{node.lineno}:{alias.name}")
    assert failures == []


def test_runtime_package_does_not_import_terminal_interface() -> None:
    failures = []
    for path in (ROOT / "nz_coder" / "runtime").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and str(node.module or "").startswith("nz_coder.interface"):
                failures.append(f"{path.relative_to(ROOT)}:{node.lineno}:{node.module}")
            if isinstance(node, ast.Import):
                failures.extend(
                    f"{path.relative_to(ROOT)}:{node.lineno}:{alias.name}"
                    for alias in node.names
                    if alias.name.startswith("nz_coder.interface")
                )
    assert failures == []
