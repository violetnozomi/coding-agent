"""Record offline validation commands without importing product code."""
import json
import subprocess
import sys
import time
from pathlib import Path

out = Path(__file__).resolve().parent
label, *argv = sys.argv[1:]
started = time.monotonic()
with (out / (label + '.txt')).open('w') as log:
    result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT)
record = dict(label=label, argv=argv, exit_code=result.returncode,
              elapsed_seconds=round(time.monotonic() - started, 3), log=label + '.txt')
with (out / 'commands.jsonl').open('a') as log:
    log.write(json.dumps(record) + '\n')
print(json.dumps(record), flush=True)
print((out / (label + '.txt')).read_text()[-2000:], flush=True)
raise SystemExit(result.returncode)
