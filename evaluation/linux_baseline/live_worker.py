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

    OpenAICompatibleProvider.create_client = create_client
    headless.RunRequest = request
    for cls in excluded_clients:
        cls.create_client = excluded_client
    try:
        yield
    finally:
        OpenAICompatibleProvider.create_client = original_client
        headless.RunRequest = original_request
        for cls, original in excluded_clients.items():
            cls.create_client = original


def run(config: dict, repo: Path, session: str, prompt: str, evidence: Path, *, inner_factory=None) -> int:
    from .live import authorized_policy
    policy = authorized_policy(config)
    if inner_factory is None and (evidence.parent.resolve() != Path(config["output_directory"]).resolve()
                                  or evidence.name not in {"T01", "T04"}):
        raise BillingStopped("Worker is outside the authorized experiment")
    (evidence / "worker-started").mkdir(mode=0o700)  # Before creating a fresh ledger or client.
    api_key = os.environ.get("API_KEY", "")
    if not api_key:
        raise BillingStopped("P1 credential missing")
    ledger = Ledger(policy, journal(evidence / "billing.jsonl"))
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.interface.cli import main as product_main

    snapshot = load_config_snapshot(repo)
    WorkspaceTrustStore().trust(repo, "workspace-control", snapshot.control_fingerprint)
    try:
        with bounded_product(policy, ledger, api_key, inner_factory=inner_factory):
            return product_main([
                "run", "--cwd", str(repo), "--provider", "openai-compatible", "--model", policy.model,
                "--permission-mode", "auto", "--session", session,
                "--max-turns", "30", "--output", "jsonl", "--prompt", prompt,
            ])
    finally:
        write_json(evidence / "billing-summary.json", ledger.snapshot())


def main(argv=None) -> int:
    # Only the serial organizer creates this descriptor, with no credentials.
    path = Path((argv or sys.argv[1:])[0]).resolve()
    descriptor = json.loads(path.read_text())
    from .live import authorized_policy, freeze
    from .catalog import TASK_SPECS
    policy = authorized_policy(descriptor["config"])
    freeze(descriptor["config"], policy)
    expected = next((spec for spec in TASK_SPECS if spec[0] == path.parent.name), None)
    if expected is None or descriptor["prompt"] != expected[4]:
        raise BillingStopped("Worker descriptor differs from the frozen task")
    return run(descriptor["config"], path.parent / "repo", descriptor["session"],
               descriptor["prompt"], path.parent)


if __name__ == "__main__":
    raise SystemExit(main())
