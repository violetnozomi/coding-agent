"""Finalize descriptive C diagnostics without a model call."""
import json
from pathlib import Path

OUT=Path(__file__).resolve().parent
P=OUT/'C/nzcoder'


def read(name):return json.loads((P/name).read_text())
def save(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


config=json.loads((OUT/'preflight/config.json').read_text())
config.update(authorized=True,authorization_source='authorization.json: latest user confirmation',
     launch_ready=True,remaining_prelaunch=[],actual_main_requests=24,actual_auxiliary_requests=2,
     status='finished; no further sample authorized',harness='C-only adaptation of historical Q transport',
     effective_config_evidence='C/nzcoder/provider-requests.jsonl',
     preflight_note='config.json preserves the earlier no-authorization preflight; this file supersedes authorization/launch status only')
save(OUT/'preflight/launch-config.json',config)
rows=read('causal-table.json');versions=read('versions.json')['versions']
for row in rows:
    previous=[v for v in versions if any(o['request_id']<=row['request_id'] for o in v['observations'])]
    row['evaluator_obligation_snapshot']=previous[-1]['user_obligations'] if previous else None
    row['evaluator_snapshot_scope']='post-run audit; not model-visible; version may still be work-in-progress'
    row['permission_outcomes']=[{'name':t['name'],'args':t['tool_input'],'permission_denied':t['permission_denied'],
                                 'dispatch_failed':t['dispatch_failed'],'executed':t['executed'],'command_failed':t['command_failed']}
                                for t in row['tool_executions']]
save(P/'causal-table.json',rows)
mutations=[]
for v in versions:
    first=v['observations'][0]
    mutations.append({'version':v['version'],'generation':first['mutation_generation'],
      'first_observed_before_request':first['request_id'],'changed_files':v['changed_files'],
      'acceptance':v['acceptance']['summary'],'project_test_exit':v['project_tests']['exit'],
      'project_test_output':v['project_tests']['stdout'],'verification_generation_at_observation':first['verification_generation'],
      'tests_audit':v['test_audit'],'spec_unchanged':v['spec_matches_original']})
save(OUT/'analysis/mutation-timeline.json',mutations)
commands=[
 {'command':'python tests/evaluation/fixtures/offline_exec.py python docs/evidence/diagnostic-v1-C-real-2026-09-22/preflight.py','exit':0,'kind':'offline','result':'V0 5 project tests; 1/12 external acceptance'},
 {'command':'python tests/evaluation/fixtures/offline_exec.py python docs/evidence/diagnostic-v1-C-real-2026-09-22/prepare-run.py','exit':0,'kind':'offline','result':'isolation checks, C-only fixture materialization'},
 {'command':'inline offline capture snapshot check','exit':0,'kind':'offline','result':'active write did not create stable version; settled snapshot copied'},
 {'command':'python docs/evidence/diagnostic-v1-C-real-2026-09-22/run-batch.py C','exit':0,'kind':'one authorized real run','result':'24 main + 2 auxiliary; no rerun'},
 {'command':'python docs/evidence/diagnostic-v1-C-real-2026-09-22/project-C.py','exit':0,'kind':'offline','result':'7 settled versions independently checked'},
 {'command':'python docs/evidence/diagnostic-v1-C-real-2026-09-22/obligation-audit.py','exit':0,'kind':'offline','result':'test/report audit, separate invalid-write diagnosis'},
]
(OUT/'commands.jsonl').write_text(''.join(json.dumps(r)+'\n' for r in commands))
print('report facts prepared')
