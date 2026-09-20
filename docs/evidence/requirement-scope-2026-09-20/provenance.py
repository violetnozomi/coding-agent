"""Read-only scope projection; no provider or agent invocation."""
import json
from dataclasses import asdict
from pathlib import Path
from nz_coder.runtime.execution.runtime_state import RuntimeState
from nz_coder.intelligence.bootstrap_artifacts import resolve_bootstrap_artifacts
from nz_coder.runtime.agent.task_contract import derive_task_contract
root = Path.cwd()
fixture = json.loads((root / 'tests/fixtures/paid_m_requirement_fragment.json').read_text())
workspace = root / 'docs/evidence/complex-paired-preflight-2026-09-20/initial/M'
state = RuntimeState()
state.set_acceptance_criteria_from_text(fixture['task'])
resolution = resolve_bootstrap_artifacts(fixture['task'], workspace=workspace,
                                       explicit_path_allowlist=tuple(state.requested_paths))
contract = derive_task_contract(fixture['task'], workspace=workspace,
    acceptance_command=state.verification_contract['command'], explicit_path_allowlist=tuple(state.requested_paths))
print(json.dumps({'task': fixture['task'], 'requested_paths': state.requested_paths,
                  'bootstrap': [asdict(x) for x in resolution.artifacts], 'task_contract': contract.to_dict()},
                 ensure_ascii=False, indent=2))
