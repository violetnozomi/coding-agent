"""Offline integrity/access/privacy audit; never invokes an agent or provider."""
import hashlib,json,subprocess
from pathlib import Path
R=Path(__file__).resolve().parent
P=R.parent/'complex-paired-preflight-2026-09-20'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def read(p):return json.loads(p.read_text())
def lines(p):return [json.loads(s) for s in p.read_text().splitlines() if s.strip()]
def hashes(p):return {str(f.relative_to(p)):sha(f) for f in sorted(p.rglob('*')) if f.is_file()}
errors=[];runs=[];access=[];privacy=[];totals={}
for name,expected in read(R/'source-hashes.json').items():
 if sha(R/name)!=expected:errors.append('execution harness changed: '+name)
for line in (P/'SHA256SUMS').read_text().splitlines():
 expected,name=line.split('  ',1)
 if sha(P/name)!=expected:errors.append('preflight changed: '+name)
for case in 'M':
 pair=[]
 for side in ('nzcoder','infcodex'):
  d=R/case/side; initial=read(d/'initial-hashes.json');final=read(d/'final-hashes.json')
  if initial!=hashes(P/'initial'/case):errors.append(f'{case}/{side}: initial mismatch')
  if final!=hashes(d/'final-files'):errors.append(f'{case}/{side}: final mismatch')
  pair.append(read(d/'initial-git.json')['head'])
  rows=read(d/'causal-table.json');requests=lines(d/'provider-requests.jsonl');responses=read(d/'normalized-responses.json')
  assert len(rows)==len(requests)==len(responses)
  assert all(x['http_status']==200 and not x['transport_error_type'] for x in responses)
  assert all(x['usage'] for x in responses)
  counts=read(d/'request-counts.json');assert counts['main']<=24 and counts['auxiliary']<=8
  opts={json.dumps(x['request_options'],sort_keys=True) for x in rows}
  commands=[];paths=[];toolnames=set()
  for row in rows:
   for c in row['response']['tool_calls']:
    toolnames.add(c['name']);a=json.loads(c['arguments'])
    if c['name']=='bash':commands.append({'request_id':row['request_id'],'command':a['command']})
    def walk_args(v):
     if isinstance(v,dict):
      for k,w in v.items():
       if k in ('path','workdir','cwd') and isinstance(w,str):paths.append(w)
       elif isinstance(w,(dict,list)):walk_args(w)
     elif isinstance(v,list):
      for w in v:walk_args(w)
    walk_args(a)
  expected_root=f'/tmp/money-paired-retest-20260920/{case}/{side}/workspace'
  outside=[p for p in paths if p.startswith('/') and not (p==expected_root or p.startswith(expected_root+'/')) or '..' in Path(p).parts]
  suspicious=[c for c in commands if any(s in c['command'] for s in ('docs/evidence','acceptance/','curl ','wget ','pip install','npm install'))]
  access.append({'case':case,'side':side,'tools':sorted(toolnames),'paths':sorted(set(paths)),'commands':commands,'outside_explicit_paths':outside,'suspicious_commands':suspicious,'manual_review':'Automated suspicious path/command checks; individual commands retained below for manual review. Not a syscall audit or hermetic filesystem guarantee.'})
  assert not outside and not suspicious
  totals.setdefault(side,{})
  for group,u in read(d/'usage.json')['totals'].items():
   target=totals[side].setdefault(group,{})
   for k,v in u.items():target[k]=target.get(k,0)+v
  runs.append({'case':case,'side':side,'counts':counts,'all_http_200':True,'usage_all_present':True,'options':[json.loads(o) for o in sorted(opts)],'changed_files':[n for n in sorted(set(initial)|set(final)) if initial.get(n)!=final.get(n)],'initial_files_verified':len(initial),'final_files_verified':len(final)})
 assert pair[0]==pair[1]
# Scan structured capture and any nested JSON strings; never output removed private text.
banned={'reasoning_content','private_reasoning','provider_extra'}
def scan(v,p,where=''):
 if isinstance(v,dict):
  for k,w in v.items():
   if k in banned:privacy.append({'file':str(p.relative_to(R)),'key':k})
   scan(w,p,where+'/'+k)
 elif isinstance(v,list):
  for w in v:scan(w,p,where)
 elif isinstance(v,str) and v[:1] in ('{','['):
  try:w=json.loads(v)
  except ValueError:return
  scan(w,p,where)
files=0
for p in R.rglob('*'):
 if p.suffix in ('.json','.jsonl'):
  files+=1
  for v in (lines(p) if p.suffix=='.jsonl' else [read(p)]):scan(v,p)
assert not privacy
assert not errors
repo=R.parents[2]
status=subprocess.check_output(['git','status','--porcelain'],cwd=repo,text=True)
assert not status
old='docs/evidence/paid-comparison-2026-09-16'
old_tracked=subprocess.check_output(['git','ls-files',old],cwd=repo,text=True).splitlines()
assert old_tracked
old_bad=[]
for name in old_tracked:
 original=subprocess.check_output(['git','show','HEAD:'+name],cwd=repo)
 if hashlib.sha256(original).hexdigest()!=sha(repo/name):old_bad.append(name)
assert not old_bad
receipt={'audit':'offline only; no model requests','nz_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo,text=True).strip(),'reference_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=repo/'references/InfCodeX',text=True).strip(),'tracked_tree_clean':True,'old_evidence_tracked_files_verified_against_HEAD':len(old_tracked),'preflight_manifest_all_hashes_valid':True,'paired_initial_git_identical':True,'structured_privacy_files_scanned':files,'private_fields_found':privacy,'runs':runs,'usage_totals':totals,'cost':'cost unknown','errors':errors}
receipt['execution_harness_hashes_unchanged']=True
historical_count=0
for dirname in ('n-retest-preflight-2026-09-19','n-real-retest-2026-09-19','complex-paired-preflight-2026-09-20','complex-paired-2026-09-20','requirement-scope-2026-09-20'):
 directory=R.parent/dirname
 for line in (directory/'SHA256SUMS').read_text().splitlines():
  expected,name=line.split('  ',1)
  assert sha(directory/name)==expected,str(directory/name)
  historical_count+=1
receipt['historical_manifest_files_unchanged']=historical_count
frozen=read(R/'manifest.json')
assert receipt['nz_commit']==frozen['nz_commit']
assert receipt['reference_commit']==frozen['infcodex_commit']
assert all(q['payload']['model']=='deepseek-v4-flash' for side in ('nzcoder','infcodex') for q in lines(R/'M'/side/'provider-requests.jsonl'))

(R/'access-audit.json').write_text(json.dumps(access,ensure_ascii=False,indent=2)+'\n')
(R/'integrity-audit.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2)+'\n')
print(json.dumps(receipt,ensure_ascii=False,indent=2))
