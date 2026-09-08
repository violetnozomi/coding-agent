"""One isolated P1 child using the real headless CLI, Provider and Native runtime."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sys

from .billing import BillingStopped, Ledger, journal, make_client
from .runner import write_json


# Explicit, frozen restricted pilot: no dynamic tools, paid embeddings, Web,
# MCP, image descriptions, handoffs, background/workflow or child Agent tools.
TOOLS = ("list_directory", "read_file", "write_file", "edit_file", "apply_patch",
         "bash", "glob_search", "grep_search", "repo_map", "read_symbol",
         "find_symbol_callers", "update_scratchpad", "read_scratchpad", "todo",
         "diff_status", "verify_changed_files")


@contextmanager
def bounded_product(policy, ledger, api_key: str, *, inner_factory=None):
    """Process-local seam; auxiliary compatible clients share the same ledger.

    Used only in the fresh single-task process. Does not replace any Agent loop.
    Every changed attribute is restored, including on construction failure.
    """
    from nz_coder.interface import headless
    from nz_coder.providers.openai_compatible import OpenAICompatibleProvider
    from nz_coder.providers import AnthropicProvider, GeminiProvider, OpenAIResponsesProvider

    original_client = OpenAICompatibleProvider.create_client
    original_completion = OpenAICompatibleProvider.create_completion
    original_request = headless.RunRequest
    excluded_clients = {cls: cls.create_client for cls in
                        (AnthropicProvider, GeminiProvider, OpenAIResponsesProvider)}

    def excluded_client(provider):
        raise BillingStopped("Provider family is outside the authorized P1 scope")

    def create_client(provider):
        if (provider.name != "openai-compatible" or provider.base_url != policy.endpoint
                or provider.api_key != api_key):
            raise BillingStopped("Uncovered P1 provider configuration")
        return make_client(policy, ledger, api_key,
                           inner=inner_factory() if inner_factory else None)

    def request(**kwargs):
        kwargs["profile"] = replace(kwargs["profile"], allow_child_agents=False,
                                    interactive_questions=False)
        kwargs["agent"] = replace(kwargs["agent"], allowed_tools=TOOLS, handoffs=())
        kwargs["tool_names"] = TOOLS
        return original_request(**kwargs)

    def create_completion(provider, client, **kwargs):
        # Public DeepSeek defaults made explicit at the request construction
        # boundary. Explicit caller fields are never replaced. Final Provider
        # normalization/SDK extra_body merges are validated by BudgetTransport.
        extra = dict(kwargs.get("extra_body") or {})
        extra.setdefault("thinking", {"type": policy.main_thinking})
        if extra["thinking"] == {"type": "enabled"}:
            kwargs.setdefault("reasoning_effort", policy.effort)
        kwargs["extra_body"] = extra
        return original_completion(provider, client, **kwargs)

    OpenAICompatibleProvider.create_client = create_client
    OpenAICompatibleProvider.create_completion = create_completion
    headless.RunRequest = request
    for cls in excluded_clients:
        cls.create_client = excluded_client
    try:
        yield
    finally:
        OpenAICompatibleProvider.create_client = original_client
        OpenAICompatibleProvider.create_completion = original_completion
        headless.RunRequest = original_request
        for cls, original in excluded_clients.items():
            cls.create_client = original


def run(config: dict, repo: Path, session: str, prompt: str, evidence: Path, *, inner_factory=None) -> int:
    from .live import authorized_policy, execution_plan
    policy = authorized_policy(config)
    if (evidence.parent.resolve() != Path(config["output_directory"]).resolve()
            or evidence.name not in execution_plan(config)):
        raise BillingStopped("Worker is outside the authorized experiment")
    if (evidence / "worker-started").exists():
        raise FileExistsError("Formal worker has already started")
    if inner_factory is None:
        descriptor = validate_descriptor(evidence / "request.json")
        if (descriptor["config"] != config or descriptor["session"] != session or descriptor["prompt"] != prompt
                or repo.resolve() != (evidence / "repo").resolve()):
            raise BillingStopped("Worker arguments differ from authorized descriptor")
    (evidence / "worker-started").mkdir(mode=0o700)  # Before creating a fresh ledger or client.
    api_key = os.environ.get("API_KEY", "")
    if not api_key:
        raise BillingStopped("P1 credential missing")
    ledger = Ledger(policy, journal(evidence / "billing.jsonl"))
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.interface.cli import main as product_main

    snapshot = load_config_snapshot(repo)
    WorkspaceTrustStore().trust(repo, "workspace-control", snapshot.control_fingerprint)
    primary = None
    try:
        with bounded_product(policy, ledger, api_key, inner_factory=inner_factory):
            return product_main([
                "run", "--cwd", str(repo), "--provider", "openai-compatible", "--model", policy.model,
                "--permission-mode", "auto", "--session", session,
                "--max-turns", "30", "--output", "jsonl", "--prompt", prompt,
            ])
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            write_json(evidence / "billing-summary.json", ledger.snapshot())
        except BaseException as error:
            ledger.blocked = True
            # The parent requires this final evidence and stops if it is absent
            # or torn. Do not obscure the request failure/cancellation here.
            if not isinstance(error, Exception) and (primary is None or isinstance(primary, Exception)):
                raise
            if primary is None:
                raise BillingStopped("Worker billing summary persistence failed") from None


def validate_descriptor(path: Path) -> dict:
    """Bind grant, task, session and frozen source before creating any client."""
    from decimal import Decimal
    from .live import authorized_policy, execution_plan, freeze
    from .catalog import TASK_SPECS, digest
    from .continuation import verify_claim
    from .billing import summarize
    path = path.resolve()
    descriptor = json.loads(path.read_text())
    config = descriptor["config"]
    policy = authorized_policy(config)
    plan = execution_plan(config)
    task = descriptor.get("task_id")
    output = Path(config["output_directory"]).resolve()
    expected = next((spec for spec in TASK_SPECS if spec[0] == task), None)
    if (task not in plan or path != output / task / "request.json" or descriptor.get("selected_tasks") != list(plan)
            or expected is None or descriptor.get("prompt") != expected[4]
            or descriptor.get("session") != f"p1-{config['experiment_id']}-{task}"):
        raise BillingStopped("Worker descriptor differs from the authorized frozen task")
    frozen = json.loads((output / "frozen.json").read_text())
    # Original two-task plan passes the serial remainder to task two. Rebuild
    # the original grant only for freeze comparison, never to reset its ledger.
    original_config = {**config, "total_budget": frozen["total_budget"]}
    original_policy = authorized_policy(original_config)
    if (descriptor.get("frozen_hash") != digest(frozen)
            or freeze(original_config, original_policy) != frozen):
        raise BillingStopped("Worker freeze or grant differs from organizer")
    remaining = original_policy.total_budget
    if plan == ("T01", "T04") and task == "T04":
        previous = output / "T01"
        rows = [json.loads(line) for line in (previous / "billing.jsonl").read_text().splitlines()]
        billing = summarize(rows, original_policy)
        result = json.loads((previous / "result.json").read_text())
        if (billing != json.loads((previous / "billing-summary.json").read_text()) or billing != result.get("billing")
                or billing["blocked"] or not billing["usage_complete"]
                or result.get("final_status") not in {"success", "functional_failure", "budget_exceeded"}):
            raise BillingStopped("Prior task does not permit the next worker")
        remaining = Decimal(billing["remaining_total_budget"])
    if policy.total_budget != remaining:
        raise BillingStopped("Worker budget differs from the serial remainder")
    if plan == ("T04",):
        verify_claim(config, frozen["carryover"])
    return descriptor


def main(argv=None, *, inner_factory=None) -> int:
    # Test-only controlled HTTP seam; the CLI has no switch to bypass validation.
    path = Path((argv or sys.argv[1:])[0]).resolve()
    descriptor = validate_descriptor(path)
    return run(descriptor["config"], path.parent / "repo", descriptor["session"],
               descriptor["prompt"], path.parent, inner_factory=inner_factory)


if __name__ == "__main__":
    raise SystemExit(main())
