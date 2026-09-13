# Task 3 — Skills governance diagnostics

## Implemented

- Added `SkillLoader.diagnostics()` with stable schema `skill-loader-diagnostics.v1`.
- Reports deterministic project/user/bundled source labels, available/conditional/disabled skills, precedence shadowing, and bounded parse errors.
- Keeps skill bodies lazy: diagnostics only uses already parsed metadata and never calls `Skill.get_body()`.
- Preserves project > user > bundled precedence, including when the selected project skill is disabled.
- Records safe generic parse errors for malformed metadata and unreadable UTF-8 files while continuing discovery.
- Keeps the existing `_parse_skill_file()` wrapper and loader APIs intact.

## Tests added

`tests/test_skill_governance.py` now covers empty roots, source rows, precedence and disabled selection, conditional skills, malformed/unreadable files, deterministic repeated diagnostics across reload, and body laziness.

## Verification

- `pytest -q tests/test_skill_governance.py tests/test_extensions.py tests/test_skill_loading.py` — 27 passed
- `python -m compileall -q nz_coder/state/skills.py` — passed
- `git diff --check` — passed

## Concerns

The parser now treats a `SKILL.md` without YAML-style frontmatter as malformed metadata, which is required for diagnostics but is stricter than the previous fallback behavior.
