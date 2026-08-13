---
description: Review the selected code or current workspace changes
allowed_tools:
  - read_file
  - list_directory
  - grep_search
  - smart_search
  - repo_map
  - bash
---
Review $ARGUMENTS. Inspect the relevant implementation and tests. Report correctness,
security, robustness, consistency, and performance findings with concrete file and line
references. Do not modify files unless the user explicitly asks for fixes.
