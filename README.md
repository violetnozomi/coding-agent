# NZ-Coder

NZ-Coder is a local repository-level coding agent prototype for bug fixing, small feature work, and Greenfield project creation. It is built from scratch in Python and focuses on the engineering runtime around LLM tool use: code search, symbol reading, safe edits, runtime state, verification planning, impact analysis, trace logging, and lightweight evaluation.

It is not a fully general autonomous software engineer or an enterprise unattended agent. The project is designed for learning, demos, and local repair / scaffold experiments on trusted repositories.

## Architecture

```text
User Task
   ↓
AgentLoop
   ↓
Tools: smart_search / read_symbol / edit_file / bash
   ↓
RuntimeState + Scratchpad + Memory
   ↓
ProjectProfile + VerificationPlanner + ImpactAnalyzer
   ↓
Patch Summary + Eval Results
```

Repository repair and Greenfield creation both run through the same loop, but they differ in strategy. Repair mode starts from repo search and narrow verification. Greenfield mode starts from requirements, blueprint, scaffold, batch file writes, acceptance planning, and project verification.

## Quickstart

Install and configure:

```bash
pip install -e .
cp .env.example .env
```

Fill `API_KEY` and `MODEL_ID` in `.env`, then start the CLI:

```bash
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

## Greenfield Project Mode

NZ-Coder supports both:
1. Repository repair mode
2. Greenfield project creation mode for small local prototypes

Greenfield flow:

```text
User requirement
  ↓
analyze_project_requirements
  ↓
create_project_blueprint
  ↓
scaffold_project
  ↓
write_files_batch (only if scaffold gaps remain)
  ↓
plan_project_acceptance
  ↓
verify_project_build
  ↓
Runnable project + README
```

FastAPI Todo API demo prompt:

```text
创建一个名为 todo_api 的 FastAPI Todo API 项目，支持 CRUD、SQLite、pytest 测试和 README
```

What the default FastAPI scaffold provides:
- in-memory Todo CRUD API
- pytest coverage for create/list/get/update/delete
- README quickstart
- low-noise verification commands
- Swagger UI at `http://localhost:8000/docs` after `uvicorn` starts

FastAPI scaffold note:
- the generated FastAPI demo currently targets Python 3.10+

What it intentionally does not do by default:
- cloud deployment
- automatic dependency installation
- overwrite existing files
- SQLite persistence in the default template

## Evaluation

Dry-run eval for repository repair tasks:

```bash
python -m nz_coder.eval_runner --tasks examples/eval_tasks --limit 3 --mode dry-run
```

Live eval for repository repair tasks:

```bash
python -m nz_coder.eval_runner --tasks examples/eval_tasks --limit 3 --mode live
```

Dry-run eval for Greenfield project creation tasks:

```bash
python -m nz_coder.eval_runner --tasks examples/project_creation_tasks --limit 3 --mode dry-run
```

Dry-run only validates task loading and result writing. Live mode is required to actually create project files and satisfy `expected_files` checks.

Live eval for Greenfield project creation tasks:

```bash
python -m nz_coder.eval_runner --tasks examples/project_creation_tasks --limit 3 --mode live
```

If `--mode auto` is used, the runner chooses live mode only when `API_KEY` is configured; otherwise it falls back to dry-run.

Results are written to:

```text
eval/results/<timestamp>.json
eval/results/<timestamp>.md
```

See [EVAL.md](EVAL.md) for setup, metrics, and example result format.

## Safety Notes

- Permission-based command safety is enforced before tool execution.
- Dangerous shell commands are blocked by policy.
- Package installs are blocked by default for benchmark repair and project generation.
- File edits through NZ-Coder tools are tracked and can be reverted.
- Greenfield scaffolding does not overwrite existing files unless `overwrite=True` is explicitly used.
- There is no OS-level sandbox, Docker isolation, or VM boundary yet.
- NZ-Coder is intended for local trusted repositories; verification depends on each repo's local tests and typecheck commands.

## Limitations

- Python support is strongest; TypeScript, Go, and Rust support are intentionally lightweight.
- Symbol tools are Python AST based; TypeScript symbol support is future work.
- Greenfield mode is meant for small local projects, not large product-grade systems.
- The default FastAPI template uses in-memory storage, even when the prompt mentions SQLite.
- The agent does not do deployment, cloud provisioning, or long-horizon autonomous project management.
- Evaluation harness is local and heuristic, not a secure multi-tenant benchmark service.
