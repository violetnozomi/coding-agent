"""Evaluator-only audits on frozen final copies; no edits or feedback to Agent."""
import ast
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
P=OUT/'C/nzcoder'
SUITE=ROOT/'tests/evaluation/fixtures/agent_core_diagnostic_v1'
s=importlib.util.spec_from_file_location('validator',SUITE/'validate.py')
validator=importlib.util.module_from_spec(s);s.loader.exec_module(validator)


def load(name):return json.loads((P/name).read_text())
def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,indent=2,ensure_ascii=False)+'\n')


diagnostic='''import json
from pathlib import Path
from tempfile import TemporaryDirectory
from configkit import Config
from configkit.store import save
with TemporaryDirectory() as td:
 p=Path(td)/"existing.json";p.write_text("keep-original-bytes")
 before=p.read_text();error=None
 try: save(p,Config("demo","localhost",False))
 except Exception as exc: error=type(exc).__name__
 after=p.read_text()
 print(json.dumps({"exception":error,"before":before,"after":after,"unchanged":before==after}))
'''
with tempfile.TemporaryDirectory(prefix='C-no-clobber-audit-') as td:
    ws=Path(td)/'workspace';validator.snapshot(P/'final-files',ws)
    result=validator.execute(['python','-c',diagnostic],ws)
save(OUT/'analysis/invalid-write-diagnosis.json',{'scope':'additional offline diagnosis, not changed acceptance and never model-visible',
     'execution':result,'observation':json.loads(result['stdout']),
     'cause':'store.save calls dumps before write, but dumps/to_dict never validate a directly constructed Config. Invalid bool port is serialized and overwrites destination.',
     'domain':'business implementation; no Runtime rollback participated'})

audit=load('test-diff-audit.json')
initial=SUITE/'C_long_horizon/workspace'
preserved=[]
for f in (initial/'tests').glob('*.py'):
    def tests(path):return {n.name:ast.dump(n,include_attributes=False) for n in ast.walk(ast.parse(path.read_text())) if isinstance(n,ast.FunctionDef) and n.name.startswith('test')}
    before,after=tests(f),tests(P/'final-files/tests'/f.name)
    preserved += [{'file':'tests/'+f.name,'function':name,'ast_unchanged':after.get(name)==body} for name,body in before.items()]
assert all(x['ast_unchanged'] for x in preserved)
audit.update(old_tests_preserved=True,old_test_function_comparison=preserved,
     new_requirement_coverage={
      'v2_read':['tests/test_parser.py:test_v1_and_v2_decode_identically','tests/test_cli.py:test_show_accepts_v2'],
      'migration':['tests/test_migrate.py:test_migrate_writes_v2_and_returns_dict','tests/test_migrate.py:test_repeated_migration_is_semantically_stable'],
      'dry_run':['tests/test_migrate.py:test_dry_run_leaves_source_bytes_unchanged','tests/test_cli.py:test_migrate_dry_run_prints_v2_without_writing'],
      'validation':['tests/test_parser.py:test_error_shape','tests/test_parser.py:test_only_first_problem_is_reported'],
      'serialization':['tests/test_writer.py:test_dumps_emits_v2_only'],
      'source_preservation':['tests/test_service.py:test_export_writes_v2','tests/test_migrate.py:test_invalid_config_does_not_create_destination']},
     meaningful_tests_obligation='pass',reason='Independent source audit: assertions exercise new behavior and all five old test functions preserve AST. 46 cases execute. Does not imply exhaustive coverage.',
     uncovered_requirement='Direct store.save(invalid Config) with an existing destination has no added test. One migration test checks an unused dest path and adds little coverage; other tests are substantive.')
save(P/'test-diff-audit.json',audit)
rows=load('causal-table.json')
tools=[(r['request_id'],t) for r in rows for t in r['tool_executions']]
exact=[(rid,t) for rid,t in tools if t['name']=='bash' and t['tool_input'].get('command')=='python -m pytest -q tests']
assert len(exact)==1 and '46 passed in 0.02s' in exact[0][1]['output'] and not exact[0][1]['command_failed']
final=load('result.json')['final_text']
report={'reported_project_test_result':True,'reported_result_matches_evidence':True,
     'reported_remaining_limitations':True,'invented_success_claims':[],
     'report_obligation':'pass','evidence':{'exact_command_http_request':exact[0][0],
       'reported_result':'46 passed in 0.02s','execution_output':exact[0][1]['output']},
     'known_failure_disclosure':'Final discloses bare pytest import failure and scratch-file limitation; prior corrected test failures no longer pending.',
     'scope':'Reported test execution is accurate, not proof of all behavior. Missing invalid direct write was never surfaced to the Agent by its tests; broad completeness implication is unsupported.'}
assert '46 passed in 0.02s' in final and 'Remaining limitations' in final and 'ModuleNotFoundError' in final
save(P/'final-report-audit.json',report)
ledger=load('user-obligations-final.json')
for item in ledger['obligations']:
    identity=item['id']
    if identity=='invalid_write_preserves_destination':
        item['status']='fail';item['evidence'] += [{'path':'analysis/invalid-write-diagnosis.json','fact':'invalid bool-port Config overwrote existing destination'}]
    elif identity=='meaningful_tests':
        item['status']='pass';item['evidence']=[{'path':'test-diff-audit.json','fact':'new behavior assertions, all original tests retained'}]
    elif identity=='old_test_coverage':
        item['status']='pass';item['evidence']=[{'path':'test-diff-audit.json','fact':'all 5 old test function ASTs preserved'}]
    elif identity in ('actual_test_report','remaining_limitations'):
        item['status']='pass';item['evidence']=[{'path':'final-report-audit.json'}]
    elif identity=='exact_project_verification':
        item['status']='pass';item['evidence']=[{'request_id':23,'command':'python -m pytest -q tests','current_mutation_generation':11,'verification_generation':11,'acceptance_generation':10}]
    elif identity=='spec_immutable':
        item['status']='pass';item['evidence']=[{'path':'versions.json','fact':'all captured versions keep original CONFIG_SPEC hash'}]
    elif identity in ('documentation','docs_error_shape'):
        item['status']='pass';item['evidence'] += [{'path':'final-files/docs/README.md','fact':'actual v1/v2 JSON, migrate/dry-run usage and error JSON present; independently read'}]
    elif identity in ('validation_types','validation_order_first_only','config_error_shape'):
        item['status']='uncertain';item['evidence'] += [{'fact':'parser path checks pass; direct Config serialization has no validation. Do not mark global validation complete.'}]
    else:
        item['status']='pass';item['evidence'] += [{'path':'final-files/','fact':'bounded check/source audit supports this obligation, not exhaustive input proof'}]
save(P/'user-obligations-final.json',ledger)
versions=load('versions.json')['versions']
for v in versions:
    a=v['test_audit'];a['meaningful_tests_obligation']='pass' if v['version'] in ('V4','V5','V6') else 'fail'
    a['reason']='Manual source audit of added assertion bodies; early V4 has two test-authoring errors' if v['version'] in ('V4','V5','V6') else 'No added tests at this version'
    for item in v['user_obligations']['obligations']:
        if item['id']=='meaningful_tests':item['status']=a['meaningful_tests_obligation']
        elif item['id']=='invalid_write_preserves_destination':item['status']='fail'
        elif item['id']=='spec_immutable':item['status']='pass' if v['spec_matches_original'] else 'fail'
        elif item['id']=='documentation':item['status']='pass' if v['version']=='V6' else 'fail'
        elif item['id'] in ('actual_test_report','remaining_limitations'):item['status']='unknown'
    # Only final ledger also includes the later final-answer obligation audit.
save(P/'versions.json',{'method':'Post-run settled snapshot checks; evaluator statuses are not Runtime ledger entries and never fed to model. Final reporting is audited separately after V6.','versions':versions})

sem=load('semantic-review.json')
authority=[]
original=(initial/'CONFIG_SPEC.md').read_text()
for review in sem:
    visible='\n'.join(str(m.get('content','')) for m in review['visible_input'])
    refs=review['runtime_before']['task_reference_evidence']
    authority.append({'request_id':review['request_id'],'original_reference_present':any(r['text']==original for r in refs),
      'authoritative_section': '=== AUTHORITATIVE TASK REFERENCES ===' in visible,
      'no_overwrite_clause_visible':'No validation error may overwrite' in visible,
      'original_text_visible':original in visible or json.dumps(original,ensure_ascii=False) in visible,
      'reference_digest':review['runtime_before']['reference_digest'],'references':refs})
save(OUT/'analysis/authority-timeline.json',{'review_visibility':authority,
     'spec_hashes':[{ 'version':v['version'],'hash':v['spec_hash'],'matches_initial':v['spec_matches_original']} for v in versions]})
first={'classification':'obligation_tracking','first_main_request':8,'first_stable_version':'V1',
     'domain':'model/business implementation omission; not proven deterministic Core mechanism',
     'observation':'First writer rewrite emits v2 but does not validate Config before serialization. Direct invalid saves remain accepted through final.',
     'why_meaningful':'The omission survives all later edits and causes the only final frozen acceptance failure; this is not the expected V0 failure.',
     'evidence':['C/nzcoder/workspace-versions/v001/configkit/config/writer.py','C/nzcoder/versions.json','analysis/invalid-write-diagnosis.json'],
     'alternative_explanations':['Agent may interpret validation as input parsing only; CONFIG_SPEC validation section has no explicit save signature, but frozen acceptance and current user obligation explicitly cover Config validation/no-clobber.',
       'At V1 the implementation was still in progress; omission becomes confirmed final mismatch only when no repair follows and completion is accepted.'],
     'confidence':{'persistent_behavioral_omission':'strong','earliest_internal_misunderstanding':'insufficient','deterministic_Core_root_cause':'insufficient'},
     'earlier_events':'Permission denials at #1/#2 are separately recorded; safe list_directory alternative used. They do not establish this omission.',
     'normal_recovery':'#16 generated two incorrect tests; #17 failure visible at #18; #20 corrected JSON True->true and nonexistent-path test; #21 green. No production source correction occurred in this recovery.'}
save(OUT/'analysis/first-divergence.json',first)
save(OUT/'analysis/obligation-analysis.json',{'classification':'C-MODEL-INCOMPLETE','behavioral_acceptance':'11/12',
     'project_test_obligation':'pass: exact current command 46 passed','test_addition_obligation':'pass',
     'documentation_obligation':'pass','final_report_obligation':'pass for actual test result and limitations',
     'runtime_terminal_state':'completed','full_task_complete':False,
     'runtime_tracked':'R1 broad behavior, R2 compatibility, R3 exact command; no distinct direct-save validation/no-clobber obligation.',
     'evaluator_observed':'Explicit write-safety gap survives; tests/reviewer missed it.',
     'core_candidate':'TaskContract granularity and semantic coverage could miss obligations, but this run does not prove deterministic extraction/control-flow corruption. Build separate offline RED before any Core patch.'})
save(OUT/'analysis/verification-timeline.json',[{'request_id':r['request_id'],'before':r['before'],'after':r['after'],
     'commands':[t for t in r['tool_executions'] if t['name']=='bash']} for r in rows if any(t['name']=='bash' for t in r['tool_executions'])])
next18=next(r for r in rows if r['request_id']==18)
failure_visible=any('2 failed, 43 passed' in str(m.get('content','')) for m in next18['model_visible_tool_results'])
save(OUT/'analysis/causal-findings.json',{'first_divergence':first,'new_deterministic_Core_bug_proven':False,
     'normal_test_failure_visible_in_next_request':failure_visible,'failure_request':17,'next_request':18,
     'semantic_review':['verifier-1 revise is irrelevant comparison/clarification text; no code mutation afterward','verifier-2 accepts with original authority visible but misses invalid direct write'],
     'destructive_review':False,'review_run_evidence':'unobserved','compaction':'not observed','timeout':'not observed',
     'next_action':'Stop. If pursuing obligation modeling, first build a separate deterministic offline regression. No extra real sample and no production patch here.'})
print(json.dumps({'meaningful_tests':'pass','old_tests_preserved':len(preserved),'report':'pass',
     'failure_feedback_visible':failure_visible,'authority':[(r['request_id'],r['original_text_visible']) for r in authority]}))
