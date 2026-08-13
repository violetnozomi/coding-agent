"""Exit after spawning a SIGTERM-resistant child in the same process group."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def main() -> None:
    pid_path = Path(sys.argv[1])
    child_code = (
        "import os,signal,sys,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "open(sys.argv[1], 'w', encoding='utf-8').write(str(os.getpid()));"
        "time.sleep(30)"
    )
    subprocess.Popen([sys.executable, "-c", child_code, str(pid_path)])
    deadline = time.monotonic() + 2
    while not pid_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


if __name__ == "__main__":
    main()
