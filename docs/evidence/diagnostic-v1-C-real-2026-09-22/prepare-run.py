"""Offline authorization/config/isolation freeze, never sends a model request."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

OUT=Path(__file__).resolve().parent
ROOT=OUT.parents[2]
sys.path.insert(0,str(ROOT))
SUITE=ROOT/'tests/evaluation/fixtures/agent_core_diagnostic_v1'
BASE='391e12d8e17f397476094cba8828c33b6b07e172'
spec=importlib.util.spec_from_file_location('validator',SUITE/'validate.py')
validator=importlib.util.module_from_spec(spec);spec.loader.exec_module(validator)
assert subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()==BASE
assert not subprocess.check_output(['git','status','--short'],cwd=ROOT)
ws=OUT/'prepared/initial/C'
initial=validator.prepare('C_long_horizon',ws)
assert initial==json.loads((OUT/'preflight/fixture-hashes.json').read_text())['workspace']
task=(SUITE/'C_long_horizon/task.md').read_text()
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
access=WorkspaceFileAccess(ws)
denials=[]
for path in [str(SUITE/'C_long_horizon/acceptance/checks.json'),str(SUITE/'C_long_horizon/oracle/configkit/cli.py'),
             '../acceptance/checks.json','../../../../preflight/user-obligations.json']:
    try: access.read_text(path,maximum=100000)
    except (OSError,ValueError) as exc:
        denials.append({'kind':'absolute_evaluator' if Path(path).is_absolute() else 'traversal', 'denied':True,'exception':type(exc).__name__})
    else: raise AssertionError('Evaluator file accessible through production read path')
def save(name,data):
    (OUT/name).write_text(json.dumps(data,indent=2)+'\n')
save('preflight/isolation.json',{'file_count':len(initial),'only_workspace_materialized':True,
     'safe_workspace_access':denials,'network':'existing inherited seccomp non-Unix denial',
     'limitations':'Not an OS filesystem jail. Production workspace policy and unchanged Bash guards remain enforced. Stop on successful evaluator access.'})
save('authorization.json',{'user_message':'确认','scope':'exactly one C_long_horizon NZ run',
     'main_cap':24,'auxiliary_cap':8,'auxiliary':'production natural calls only','retries_count':True,
     'excluded':['A','B','M','Q','S','InfCodeX','second C'],'granted':True})
save('manifest.json',{'nz_commit':BASE,'tasks':[{'id':'C','prompt':task,'initial_hashes':initial}],
     'acceptance':'frozen C_long_horizon/acceptance/checks.json','oracle_exposed':False})
save('preflight/harness-hashes.json',{name:hashlib.sha256((OUT/name).read_bytes()).hexdigest()
     for name in ['run-nz.py','run-batch.py','capture.py','analyze.py','freeze.py']})
assert (OUT/'run-nz.py').read_bytes()==(ROOT/'docs/evidence/q-post-stream-timeout-real-retest-2026-09-22/run-nz.py').read_bytes()
print('C-only prepare passed; production file access rejects evaluator paths; run-nz byte-identical to Q; 0 requests')
