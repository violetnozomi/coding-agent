"""Audit and index one completed C run; never execute an Agent."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from capture import clean
from freeze import scrub

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
P=OUT/'C/nzcoder'
BASE='391e12d8e17f397476094cba8828c33b6b07e172'


def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def load(path):return json.loads(path.read_text())
def save(path,data):path.write_text(json.dumps(data,indent=2,ensure_ascii=False)+'\n')
def lines(path):return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


for d in OUT.rglob('__pycache__'):shutil.rmtree(d)
# Only unfrozen evaluator projections are normalized here.
for d in (OUT/'analysis', OUT/'preflight'):
    for f in d.rglob('*.json'):
        save(f,scrub(load(f)))
frozen=load(P/'FROZEN.json')['sha256']
assert all(sha(P/name)==digest for name,digest in frozen.items())
sources=load(OUT/'preflight/source-hashes.json')
assert all(sha(ROOT/name)==digest for name,digest in sources.items())
old=load(ROOT/'docs/evidence/agent-core-diagnostic-suite-v1-2026-09-22/SHA256SUMS.json')
assert all(sha(ROOT/name)==digest for name,digest in old.items())
changed=subprocess.check_output(['git','diff',BASE,'--name-only'],cwd=ROOT,text=True).splitlines()
assert all(x.startswith(str(OUT.relative_to(ROOT))+'/') for x in changed)
requests=lines(P/'provider-requests.jsonl');responses=lines(P/'provider-responses.jsonl')
assert len(requests)==len(responses)==26
assert [q['request_id'] for q in requests]==list(range(1,27))
assert sum(q['purpose']=='coding' for q in requests)==24
assert sum(q['purpose']=='verifier' for q in requests)==2
assert all(r['status']==200 and (r.get('done') or r.get('json',{}).get('choices')) for r in responses)
task=(ROOT/'tests/evaluation/fixtures/agent_core_diagnostic_v1/C_long_horizon/task.md').read_text()
first_users=[m['content'] for m in requests[0]['payload']['messages'] if m['role']=='user']
assert load(P/'command.json')['env']['TASK_PROMPT']==task
assert any(isinstance(text,str) and text.endswith(task) for text in first_users)
assert all(q['payload']['model']=='deepseek-v4-flash' for q in requests)
assert all(q['payload']['stream'] for q in requests if q['purpose']=='coding')
assert all(q['payload']['max_tokens']==64000 and 'thinking' not in q['payload'] and 'reasoning_effort' not in q['payload'] for q in requests if q['purpose']=='coding')
assert not (P/'isolation-stop.jsonl').exists()
initial=load(P/'initial-hashes.json');versions=load(P/'versions.json')['versions']
assert all(v['spec_hash']==initial['CONFIG_SPEC.md'] for v in versions)
assert all(not o['transaction_active'] for v in versions for o in v['observations'])
assert all(sha(P/'final-files'/name)==digest for name,digest in load(P/'final-hashes.json').items())
assert (OUT/'run-nz.py').read_bytes()==(ROOT/'docs/evidence/q-post-stream-timeout-real-retest-2026-09-22/run-nz.py').read_bytes()
keys=set()
for key,value in os.environ.items():
    if re.search('KEY|TOKEN|PASSWORD|SECRET',key,re.I) and len(value)>=16:keys.add(value.encode())
for line in (ROOT/'.env').read_text().splitlines():
    key,sep,value=line.partition('=');value=value.strip().strip('\"\'')
    if sep and re.search('KEY|TOKEN|PASSWORD|SECRET',key,re.I) and len(value)>=16:keys.add(value.encode())
failures=[]
private={'reasoning_content','_nz_provider_reasoning_content','private_reasoning','api_key','authorization','cookie','set-cookie','access_token'}
def inspect(value,label):
    if isinstance(value,dict):
        for key,item in value.items():
            if key.lower() in private and item:failures.append(label+':private:'+key)
            inspect(item,label)
    elif isinstance(value,list):
        for item in value:inspect(item,label)
files=[f for f in OUT.rglob('*') if f.is_file() and f.name not in {'SHA256SUMS.json','integrity-audit.json'}]
for f in files:
    data=f.read_bytes();name=str(f.relative_to(OUT))
    if str(Path.home()).encode() in data:failures.append(name+':personal-path')
    if any(k in data for k in keys):failures.append(name+':credential')
    if re.search(rb'\bsk-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |OPENSSH )?PRIVATE KEY-----',data):failures.append(name+':credential-pattern')
    if f.suffix=='.json':inspect(load(f),name)
    if f.suffix=='.jsonl':
        for row in lines(f):inspect(row,name)
assert not failures,failures
result={'passed':True,'baseline':BASE,'source_files_unchanged':len(sources),
        'suite_frozen_files_unchanged':len(old),'old_tracked_evidence_diff':False,
        'frozen_run_files':len(frozen),'scanned_files':len(files),
        'main_requests':24,'auxiliary_requests':2,'extra_samples':0,'provider_errors':0,
        'initial_task_byte_exact':True,'settled_versions':len(versions),'spec_unchanged':True,
        'production_changes':False,'privacy_findings':failures,'cost':'unknown',
        'limitation':'Pattern-based scans cannot prove absence of every unknown secret; network isolation is not filesystem hermeticity.'}
save(OUT/'integrity-audit.json',result)
# Index every file other than root index itself (including frozen and preflight indices).
save(OUT/'SHA256SUMS.json',{str(f.relative_to(OUT)):sha(f) for f in sorted(OUT.rglob('*'))
     if f.is_file() and f != OUT/'SHA256SUMS.json'})
print(json.dumps(result,indent=2))
