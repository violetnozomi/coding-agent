"""Run one local check with exact argv, output, duration and exit record."""
import json
from pathlib import Path
import subprocess
import sys
import time
root=Path(__file__).resolve().parent
label,*argv=sys.argv[1:]
start=time.monotonic()
with (root/(label+'.txt')).open('w') as log:
    process=subprocess.run(argv,stdout=log,stderr=subprocess.STDOUT)
entry={'label':label,'cwd':str(Path.cwd()),'argv':argv,'exit_code':process.returncode,'elapsed_seconds':round(time.monotonic()-start,3),'log':label+'.txt'}
with (root/'commands.jsonl').open('a') as stream:stream.write(json.dumps(entry)+'\n')
print(json.dumps(entry))
print('\n'.join((root/(label+'.txt')).read_text().splitlines()[-12:]))
sys.exit(process.returncode)
