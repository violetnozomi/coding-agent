"""Bounded clean-baseline recheck of the independently observed fake-loop stall."""
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
BASE = '64a6bba2983f363b14ebb1a936d3715ef87ff2a5'
test_name = sys.argv[1] if len(sys.argv) > 1 else 'test_go_on_resumes_inactive_max_turns_task_state_with_fresh_budget'
log_name = 'baseline-stall-isolated.txt' if len(sys.argv) > 1 else 'baseline-loop-isolated.txt'
with tempfile.TemporaryDirectory(prefix='call-role-baseline-') as td:
    archive = subprocess.check_output(['git', 'archive', BASE, 'nz_coder', 'tests', 'pyproject.toml'], cwd=ROOT, timeout=30)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(td, filter='data')
    command = ['timeout', '--signal=TERM', '--kill-after=5s', '75s', 'python',
        'tests/evaluation/fixtures/offline_exec.py', 'python', '-m', 'pytest', '-vv',
        '-o', 'faulthandler_timeout=40',
        'tests/test_loop_fake.py::' + test_name]
    with (OUT / 'after' / log_name).open('w') as log:
        result = subprocess.run(command, cwd=td, stdout=log, stderr=subprocess.STDOUT, timeout=90)
        log.write(f'\nBounded baseline exit: {result.returncode}\n')
