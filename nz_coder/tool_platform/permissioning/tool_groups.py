"""Compatibility tool groupings and explicit safe state-tool exceptions."""
from __future__ import annotations

from nz_coder.tools import FILESYSTEM_MUTATION_TOOLS


# Backward-compatible exports. Runtime authority comes from registry metadata;
# these snapshots remain for extensions importing the historic names.
WRITE_TOOLS = FILESYSTEM_MUTATION_TOOLS

READ_TOOLS = frozenset({
    "read_file",
    "list_directory",
    "grep_search",
    "glob_search",
    "repo_map",
    "code_references",
    "lsp",
    "list_memories",
    "project_profile",
    "plan_verification",
    "analyze_impact",
    "analyze_project_requirements",
    "create_project_blueprint",
    "inspect_generated_project",
    "check_project_completeness",
    "plan_project_acceptance",
    "verify_project_build",
    "webfetch",
})

SAFE_STATE_TOOLS = frozenset({
    "todo",
    "compact",
    "emit_handoff",
    "question",
    "plan_enter",
    "write_plan",
    "plan_exit",
    "task",
    "agent_manager",
    "load_skill",
    "recall_memory",
    "read_scratchpad",
    "load_optional_tools",
    "verify_changed_files",
    "verify_project_build",
})

SAFE_TOOLS = READ_TOOLS | SAFE_STATE_TOOLS
