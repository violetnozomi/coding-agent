"""Tool groupings used by permission checks."""
from __future__ import annotations


WRITE_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "apply_patch",
    "replace_lines",
    "python_structural_edit",
    "write_files_batch",
    "scaffold_project",
    "apply_agent_changes",
    "save_memory",
    "delete_memory",
})

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

SAFE_TOOLS = READ_TOOLS | frozenset({
    "todo",
    "compact",
    "question",
    "plan_enter",
    "write_plan",
    "plan_exit",
    "task",
    "agent_manager",
    "load_skill",
    "recall_memory",
    "read_scratchpad",
})
