"""Post-run unmodified test subset on a disposable frozen copy; no model replay."""
import json,os,shutil,subprocess,sys,tempfile,time
from pathlib import Path
OUT=Path(__file__).resolve().parent;ROOT=OUT.parents[2]
node=Path.home()/'.local/lib/python3.13/site-packages/playwright/driver/node'
rows=[]
with tempfile.TemporaryDirectory(prefix='q-postrun-diagnosis-') as td:
 ws=Path(td)/'workspace';shutil.copytree(OUT/'Q/nzcoder/final-files',ws)
 for label,pattern,file in [('pool-drain','^drains already-started work','tests/pool.test.cjs'),('batch-drain','^propagates send rejections','tests/batch.test.cjs')]:
  argv=[sys.executable,str(ROOT/'tests/evaluation/fixtures/offline_exec.py'),str(node),'--test','--test-timeout=2000','--test-name-pattern='+pattern,file]
  start=time.monotonic()
  try:
   p=subprocess.run(argv,cwd=ws,env={'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'},capture_output=True,text=True,timeout=8)
   row={'label':label,'argv':argv,'exit_code':p.returncode,'stdout':p.stdout,'stderr':p.stderr}
  except subprocess.TimeoutExpired as e:
   row={'label':label,'argv':argv,'exit_code':None,'timeout':True,'stdout':(e.stdout or b'').decode() if isinstance(e.stdout,bytes) else e.stdout,'stderr':(e.stderr or b'').decode() if isinstance(e.stderr,bytes) else e.stderr}
  row['elapsed_s']=time.monotonic()-start;rows.append(row)
(OUT/'analysis/Q-unmodified-test-diagnosis.json').write_text(json.dumps({'scope':'Post-run diagnostic only; same final files, test-name filter and bounded timeout; no fixture edits and no provider calls','results':rows},indent=2)+'\n')
print([(r['label'],r['exit_code'],r['elapsed_s']) for r in rows])
