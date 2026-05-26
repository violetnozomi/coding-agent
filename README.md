# NZ-Coder

NZ-Coder is a repository-level terminal coding agent built from scratch in Python. It focuses on the engineering runtime around LLM tool use: code search, symbol reading, safe edits, runtime state, verification planning, impact analysis, trace logging, and lightweight evaluation.

It is intentionally not an enterprise unattended software engineer. The project is designed for learning, demos, and SWE-bench-style local repair experiments.

## Architecture

```text
User Task
   |
   v
AgentLoop (ReAct / CodeAct loop)
   |
   v
Tools: smart_search / read_symbol / edit_file / bash / task
   |
   v
RuntimeState + Scratchpad + Persistent Memory
   |
   v
ProjectProfile + VerificationPlanner + ImpactAnalyzer
   |
   v
Patch Summary + Verification + Evaluation Results
```

Key modules:

- `AgentLoop`: model -> tool calls -> tool results -> continue.
- `repo_intel`: `smart_search`, `read_symbol`, `find_symbol_callers`, `diff_status`, `verify_changed_files`.
- `ProjectProfile`: detects languages, package managers, roots, and common commands.
- `VerificationPlanner`: recommends minimal verification commands before broad tests.
- `ImpactAnalyzer`: estimates patch risk and suggests review checks.
- `RuntimeState`: injects objective state reminders to avoid idle loops and missed verification.
- `ChangeTracker` + `TransactionManager`: records agent-authored diffs and supports rollback.

## Quickstart

```bash
pip install -e .
cp .env.example .env  # fill API_KEY / MODEL_ID if you want live agent runs
nz-coder
```

Inside the REPL:

```text
/profile     # show detected project profile
/status      # workspace and runtime status
/diff        # latest agent-authored diff
/revert-last # revert latest tracked change set
```

## Demo Commands

Inspect project profile without calling a model:

```bash
python - <<'PYCODE'
import nz_coder.project_profile
from nz_coder.tools import dispatch
print(dispatch('project_profile', {'save': False}))
PYCODE
```

Plan verification for current changes:

```bash
python - <<'PYCODE'
import nz_coder.verification_planner
from nz_coder.tools import dispatch
print(dispatch('plan_verification', {}))
PYCODE
```

Analyze current patch risk:

```bash
python - <<'PYCODE'
import nz_coder.impact_analyzer
from nz_coder.tools import dispatch
print(dispatch('analyze_impact', {}))
PYCODE
```

## Evaluation

Run the lightweight local eval harness:

```bash
python -m nz_coder.eval_runner --tasks examples/eval_tasks --limit 3
```

If `API_KEY` is not configured, the runner automatically uses `dry-run` mode and still writes structured result files. To force a live agent run:

```bash
python -m nz_coder.eval_runner --tasks examples/eval_tasks --limit 3 --mode live
```

Results are written to:

```text
eval/results/<timestamp>.json
eval/results/<timestamp>.md
```

See [EVAL.md](EVAL.md) for setup, metrics, and example result table.

## Safety Notes

- Dangerous shell commands are denied by policy.
- Package installs are blocked by default for benchmark repair.
- File edits through NZ-Coder tools are tracked and can be reverted.
- There is no full OS-level sandbox, Docker isolation, or VM boundary.
- Verification depends on each repository's available local test/typecheck commands.

## Limitations

- Python support is strongest; TypeScript, Go, and Rust support is intentionally lightweight.
- Symbol tools are Python AST based; TypeScript symbol support is future work.
- Evaluation harness is local and heuristic, not a secure multi-tenant benchmark service.
- The agent can still make poor choices if the model ignores tool guidance or project tests are incomplete.
