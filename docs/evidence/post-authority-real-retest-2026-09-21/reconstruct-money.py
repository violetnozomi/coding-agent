"""Reconstruct serial Money mutations from executed tool inputs, verify against captured batch bytes."""
import hashlib,json,shutil
from pathlib import Path
from project import OUT,PRE,acceptance,state_facts,read,save
p=OUT/'M/nzcoder';dest=OUT/'analysis/M-generations';dest.mkdir(exist_ok=True)
snaps=read(p/'state-snapshots.jsonl');byseq={s['seq']:s for s in snaps}
files={str(f.relative_to(PRE/'initial/M')):f.read_bytes() for f in (PRE/'initial/M').rglob('*') if f.is_file()}
rows=[]
def record(generation,request_id,source):
    target=dest/f'G{generation:03}';target.mkdir(exist_ok=True)
    hashes={n:hashlib.sha256(b).hexdigest() for n,b in sorted(files.items())}
    for n,b in files.items():
        f=target/n;f.parent.mkdir(parents=True,exist_ok=True);f.write_bytes(b)
    result=acceptance('M',target,hashes)
    rows.append({'generation':generation,'main_request_id':request_id,'source':source,'source_hashes':hashes,'acceptance':result})
record(0,0,{'kind':'historical initial bytes'})
for event in read(p/'tool-results.jsonl'):
    t=event['result']
    if not (t['is_write'] and t['executed'] and not t['dispatch_failed'] and not t['command_failed']):continue
    args=t['tool_input'];path=args['path'];name=t['name']
    if name=='write_file':files[path]=args['content'].encode()
    else:
        text=files[path].decode()
        edits=[args] if name=='edit_file' else args['changes'] if name=='apply_patch' else None
        assert edits is not None,name
        for edit in edits:
            assert edit.get('op','replace')=='replace',edit
            old,new=edit['old_text'],edit['new_text']
            assert text.count(old)==1,(path,text.count(old))
            text=text.replace(old,new,1)
        files[path]=text.encode()
    before=byseq[event['before_snapshot']];after=byseq[event['after_snapshot']]
    g=after['state']['mutation_generation'];assert g==before['state']['mutation_generation']+1
    record(g,event['request_id'],{'kind':'reconstructed from successful serial tool result; not a live per-write filesystem snapshot','tool_result_after_snapshot':event['after_snapshot'],'before':state_facts(before['state']),'after':state_facts(after['state'])})
# Validate each available next-request/final settled state against reconstructed exact bytes.
checks=[]
for s in snaps:
    if s['reason'] not in ('before_provider_http_request','final_result'):continue
    g=s['state']['mutation_generation'];row=next(x for x in rows if x['generation']==g)
    assert row['source_hashes']==s['workspace_hashes'],(s['seq'],g)
    checks.append({'snapshot':s['seq'],'generation':g,'hashes_match':True})
assert rows[-1]['source_hashes']==json.loads((p/'final-hashes.json').read_text())
save(OUT/'analysis/money-generation-timeline.json',{'method':'Executed serial mutation arguments replayed offline with unique exact replacements; all next-request/final settled snapshots and final hashes independently cross-checked. No acceptance feedback to models.','rows':rows,'settled_hash_checks':checks})
print([(r['generation'],r['acceptance']['stdout'].splitlines()[-1]) for r in rows])
