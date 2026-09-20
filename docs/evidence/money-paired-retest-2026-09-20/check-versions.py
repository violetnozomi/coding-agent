"""Post-run frozen-version acceptance; no feedback to either agent or provider."""
import hashlib,json,os,shutil,subprocess,sys,tempfile
from pathlib import Path
R=Path(__file__).resolve().parent
REPO=R.parents[2]
ACCEPT=R.parent/'complex-paired-preflight-2026-09-20/acceptance/M.py'
ISOLATE=REPO/'tests/evaluation/fixtures/offline_exec.py'
rows=[]
for version in sorted((R/'M/nzcoder/transport/workspace-versions').iterdir()):
 expected=json.loads((version/'hashes.json').read_text())
 assert all(hashlib.sha256((version/n).read_bytes()).hexdigest()==v for n,v in expected.items())
 with tempfile.TemporaryDirectory(prefix='money-version-check-') as td:
  ws=Path(td)/'workspace';ws.mkdir()
  for name in expected:
   target=ws/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(version/name,target)
  env={'PATH':str(Path(sys.executable).parent)+':/usr/bin:/bin','PYTHONPATH':str(ws),'TASK_WORKSPACE':str(ws),'LANG':'C.UTF-8','PYTHONDONTWRITEBYTECODE':'1','PYTEST_DISABLE_PLUGIN_AUTOLOAD':'1'}
  cmd=[sys.executable,str(ISOLATE),sys.executable,'-m','pytest','-q','-p','no:cacheprovider',str(ACCEPT)]
  result=subprocess.run(cmd,cwd=ws,env=env,text=True,capture_output=True,timeout=45)
  rows.append({'version':version.name,'source_hashes':expected,'argv':cmd,'exit_code':result.returncode,'stdout':result.stdout,'stderr':result.stderr})
  print(version.name,result.returncode,result.stdout.splitlines()[-1])
(R/'intermediate-acceptance.json').write_text(json.dumps({'timing':'Post-run offline checks only; results never sent to main or auxiliary models','acceptance_source':str(ACCEPT),'acceptance_sha256':hashlib.sha256(ACCEPT.read_bytes()).hexdigest(),'versions':rows},ensure_ascii=False,indent=2)+'\n')
