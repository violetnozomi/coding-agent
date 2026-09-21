"""Re-run final regressions with the exact baseline stream primitive, offline.

No working-tree production file is reverted. All other components are identical
because the only production diff is this module's consumer clock accounting.
"""
import subprocess
import sys

import pytest


class BaselineStream:
    def pytest_configure(self, config):
        from nz_coder.runtime.model_gateway import gateway, stream
        source = subprocess.check_output([
            'git', 'show',
            '4e59fe75032dd32186b73d92b7ad98c955866176:nz_coder/runtime/model_gateway/stream.py',
        ], text=True)
        exec(compile(source, 'baseline/nz_coder/runtime/model_gateway/stream.py', 'exec'), stream.__dict__)
        gateway.iter_stream_with_timeouts = stream.iter_stream_with_timeouts


raise SystemExit(pytest.main(sys.argv[1:], plugins=[BaselineStream()]))
