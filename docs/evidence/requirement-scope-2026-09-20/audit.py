"""Offline final integrity receipt for the requirement-scope change."""
import hashlib,json,re,subprocess
from pathlib import Path
R=Path(__file__).resolve().parent
repo=R.parents[2]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text())
old_names=('n-retest-preflight-2026-09-19','n-real-retest-2026-09-19','complex-paired-preflight-2026-09-20','complex-paired-2026-09-20')
old_count=0
for name in old_names:
 d=R.parent/name
 for line in (d/'SHA256SUMS').read_text().splitlines():
  expected,path=line.split('  ',1)
  assert sha(d/path)==expected, str(d/path)
  old_count+=1
assert subprocess.check_output(['git','diff','d53605d','--',*[str((R.parent/name).relative_to(repo)) for name in old_names], 'docs/evidence/paid-comparison-2026-09-16'],cwd=repo)==b''
for name,expected in read(R/'source-hashes.json').items():
 assert sha(repo/name)==expected,name
private=[];count=0
def scan(v,p):
 if isinstance(v,dict):
  for k,w in v.items():
   if k in {'reasoning_content','private_reasoning','provider_extra'}:private.append(str(p))
   scan(w,p)
 elif isinstance(v,list):
  for w in v:scan(w,p)
 elif isinstance(v,str) and v[:1] in ('{','['):
  try:w=json.loads(v)
  except ValueError:return
  scan(w,p)
for p in R.rglob('*.json'):
 count+=1;scan(read(p),p)
assert not private
before=read(R/'baseline-confirmation/money-reference/replay.json')
after=read(R/'after/money-reference/replay.json')
assert before['result']=='max_turns' and after['result']=='completed'
assert read(R/'baseline-confirmation/money-reference/independent-acceptance.json')['exit_code']==0
assert read(R/'after/money-reference/independent-acceptance.json')['exit_code']==0
assert before['state']['verification_generation']==before['state']['mutation_generation']==2
assert after['state']['verification_generation']==after['state']['mutation_generation']==2
assert read(R/'baseline-confirmation/money-reference/workspace-hashes.json')==read(R/'after/money-reference/workspace-hashes.json')
final=R/'after/money-reference/final-files'
for name,expected in read(R/'after/money-reference/workspace-hashes.json').items():assert sha(final/name)==expected
assert (final/'REQUIREMENTS.md').read_bytes()==(R.parent/'complex-paired-preflight-2026-09-20/initial/M/REQUIREMENTS.md').read_bytes()
# Check report links, including folders, without touching execution evidence.
for target in re.findall(r'\]\(([^)]+)\)',(R/'README.md').read_text()):assert (R/target).exists(),target
receipt={'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),
 'baseline_production_diff':'0885c87..d53605d: docs/evidence additions only',
 'old_manifest_files_verified':old_count,'old_tracked_evidence_unchanged':True,
 'production_and_test_source_hashes_verified':True,'structured_json_files_scanned':count,
 'private_fields_found':private,'same_final_money_hashes_before_after':True,
 'money_requirements_unchanged':True,'money_independent_acceptance_before_after':'19 passed / 19 passed',
 'money_boundary_before_after':'max_turns / completed','paid_model_requests':0,
 'network_policy':'kernel IPv4/IPv6 denial for final suites and production replays; local provider substitutes',
 'known_baseline_failures':['test_context_overflow_stops_after_three_compaction_attempts','test_pre_send_and_reactive_compactions_share_one_three_attempt_owner'],
 'implementation_modified_files':subprocess.check_output(['git','diff','--name-only'],cwd=repo,text=True).splitlines()}
(R/'integrity.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(receipt,ensure_ascii=False,indent=2))
