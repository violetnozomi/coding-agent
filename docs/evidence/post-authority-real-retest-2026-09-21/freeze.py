"""Freeze one completed run; redact recorded host paths and private fields only."""
import hashlib,json,sys
from pathlib import Path
from capture import clean
OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]

def scrub(value):
    value=clean(value)
    if isinstance(value,dict):return {k:scrub(v) for k,v in value.items()}
    if isinstance(value,list):return [scrub(v) for v in value]
    if isinstance(value,str):return value.replace(str(ROOT),'<REPO>').replace(str(Path.home()),'<USER_HOME>')
    return value

def freeze(case):
    p=OUT/case/'nzcoder'
    assert not (p/'FROZEN.json').exists()
    for f in p.rglob('*'):
        if not f.is_file() or 'workspace-versions' in f.parts or 'final-files' in f.parts:continue
        if f.suffix=='.json':f.write_text(json.dumps(scrub(json.loads(f.read_text())),ensure_ascii=False,indent=2)+'\n')
        elif f.suffix=='.jsonl':f.write_text(''.join(json.dumps(scrub(json.loads(l)),ensure_ascii=False)+'\n' for l in f.read_text().splitlines()))
        elif f.suffix in ('.md','.txt','.patch','.diff'):f.write_text(scrub(f.read_text()))
    manifest={str(f.relative_to(p)):hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted(p.rglob('*')) if f.is_file()}
    (p/'FROZEN.json').write_text(json.dumps({'case':case,'sha256':manifest,'redaction':'private reasoning keys removed at capture; recorded host paths replaced by <REPO>/<USER_HOME>; workspace source bytes not rewritten'},indent=2)+'\n')
    print('frozen',case,len(manifest))
if __name__=='__main__':freeze(sys.argv[1])
