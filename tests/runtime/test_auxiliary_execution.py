"""真实双流、业务中断与物理退役；故障只控制外部I/O边界。"""

from dataclasses import replace
import sys
import threading
import time

import pytest

from nz_coder.runtime.core.run_settings import RunSettings
from tests.fixtures.process_lifecycle import make_auxiliary_runner, wait_until


def argv(script):
    compile(script, "<auxiliary-test>", "exec")
    return (sys.executable, "-u", "-c", script)


def capture_in_thread(runner, workspace, script, *, on_line=None):
    results, errors = [], []

    def run():
        try:
            method = runner.capture if on_line is None else runner.lines
            kwargs = {} if on_line is None else {"on_line": on_line}
            results.append(method(argv(script), cwd=workspace, timeout=3, **kwargs))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    return thread, results, errors


def finish(runner, thread=None):
    assert runner.close(deadline=time.monotonic() + 3)
    if thread is not None:
        thread.join(3)
        assert not thread.is_alive()
    assert runner.retired


def test_capture_keeps_stdout_stderr_and_text_semantics(tmp_path):
    runner = make_auxiliary_runner(tmp_path)
    try:
        result = runner.capture(
            argv(
                "import os; os.write(1,b'a\\r\\nb\\r\\xff'); os.write(2,b' err\\r\\n'); raise SystemExit(7)"
            ),
            cwd=tmp_path,
            timeout=2,
        )
        assert result.stdout == "a\nb\n�"
        assert result.stderr == " err\n"
        assert result.returncode == 7
        result = runner.capture(argv("print('second')"), cwd=tmp_path, timeout=2)
        assert result.stdout == "second\n"
    finally:
        finish(runner)


@pytest.mark.parametrize("early", [False, True])
def test_lines_preserves_split_unicode_tail_and_early_stop(tmp_path, early):
    settings = replace(
        RunSettings.from_legacy_globals(), bash_output_hard_limit_bytes=2 * 1024 * 1024
    )
    runner = make_auxiliary_runner(tmp_path, settings=settings)
    lines = []

    def consume(line):
        lines.append(line)
        return early and len(lines) == 2

    script = "import os; os.write(1,b'x'*(65*8192)+b'\\n'+b' '*(8191)+'中'.encode()+b'\\n\\n tail '); os.write(2,b' warn \\n')"
    try:
        result = runner.lines(argv(script), cwd=tmp_path, timeout=2, on_line=consume)
        assert lines[:2] == ["x" * (65 * 8192), "中"]
        assert lines == (lines[:2] if early else [lines[0], "中", "tail"])
        assert result.stopped_early == early
        assert not runner._cancel.is_set()
        if not early:
            assert result.stderr == "warn" and result.returncode == 0
    finally:
        finish(runner)


def test_shared_output_limit_and_early_discard(tmp_path):
    from nz_coder.protocol.public_error import PublicRuntimeError

    settings = replace(
        RunSettings.from_legacy_globals(), bash_output_hard_limit_bytes=16384
    )
    runner = make_auxiliary_runner(tmp_path, settings=settings)
    try:
        with pytest.raises(PublicRuntimeError) as exc:
            runner.capture(
                argv('import os; os.write(1,b"x"*10240); os.write(2,b"y"*10240)'),
                cwd=tmp_path,
                timeout=2,
            )
        assert exc.value.public_error.code == "auxiliary_output_limit"
    finally:
        finish(runner)
    runner = make_auxiliary_runner(tmp_path, settings=settings)
    try:

        def consume(line):
            (tmp_path / "go").touch()
            return True

        script = 'import os,time,pathlib; print("one",flush=True)\nwhile not pathlib.Path("go").exists(): time.sleep(.01)\ntime.sleep(.02)\nwhile True: os.write(1,b"x"*8192); os.write(2,b"y"*8192)'
        result = runner.lines(argv(script), cwd=tmp_path, timeout=2, on_line=consume)
        assert result.stopped_early and not runner._cancel.is_set()
    finally:
        finish(runner)


@pytest.mark.parametrize(
    "case", ["deadline", "cancel", "root", "escape", "seal", "busy"]
)
def test_auxiliary_admission_deadline_root_and_seal(tmp_path, monkeypatch, case):
    from nz_coder.runtime.process import auxiliary

    runner = make_auxiliary_runner(tmp_path, timeout=-1 if case == "deadline" else 5)
    cwd = tmp_path
    calls = []
    real = auxiliary.subprocess.Popen

    def popen(*a, **kw):
        calls.append(a)
        return real(*a, **kw)

    monkeypatch.setattr(auxiliary.subprocess, "Popen", popen)
    thread = None
    try:
        if case == "cancel":
            runner._cancel.set()
        if case == "seal":
            runner.seal()
        if case == "root":
            tmp_path.rename(tmp_path.with_name(tmp_path.name + "-old"))
            tmp_path.mkdir()
        if case == "escape":
            cwd = tmp_path.parent
        if case == "busy":
            thread, _, _ = capture_in_thread(
                runner,
                tmp_path,
                'import time; print("ready",flush=True); time.sleep(30)',
            )
            wait_until(lambda: bool(calls))
        with pytest.raises(Exception):
            runner.capture(argv('print("unexpected")'), cwd=cwd, timeout=1)
        assert len(calls) == (1 if case == "busy" else 0)
    finally:
        finish(runner, thread)


@pytest.mark.parametrize(
    "case", ["owner_construct", "owner_start", "reader_construct", "reader_start"]
)
def test_partial_launch_never_loses_native_owner(tmp_path, monkeypatch, case):
    from nz_coder.runtime.process import auxiliary

    runner = make_auxiliary_runner(tmp_path)
    real_thread, real_start = threading.Thread, threading.Thread.start

    def construct(*a, **kw):
        reader = "reader" in kw.get("name", "")
        if (case == "owner_construct" and not reader) or (
            case == "reader_construct" and reader
        ):
            raise OSError("controlled construction")
        return real_thread(*a, **kw)

    def start(self):
        real_start(self)
        if (case == "owner_start" and "reader" not in self.name) or (
            case == "reader_start" and "reader" in self.name
        ):
            raise OSError("controlled post-start")

    monkeypatch.setattr(auxiliary.threading, "Thread", construct)
    monkeypatch.setattr(real_thread, "start", start)
    try:
        with pytest.raises(Exception):
            runner.capture(argv("import time; time.sleep(30)"), cwd=tmp_path, timeout=1)
    finally:
        finish(runner)


@pytest.mark.parametrize("fault", ["poll", "wait", "sweep", "reader", "close", "owner"])
def test_native_fault_recovery_requires_physical_exit(tmp_path, monkeypatch, fault):
    from nz_coder.runtime.process import auxiliary

    runner = make_auxiliary_runner(tmp_path)
    blocked, reached = threading.Event(), threading.Event()
    blocked.set()
    real_popen, real_sweep = (
        auxiliary.subprocess.Popen,
        auxiliary.terminate_process_tree,
    )

    class Stream:
        def __init__(self, original):
            self.original = original

        def read(self, size):
            assert size <= 8192
            data = self.original.read(size)
            if fault == "reader" and not data:
                reached.set()
                while blocked.is_set():
                    time.sleep(0.005)
            return data

        def close(self):
            if fault == "close" and blocked.is_set():
                reached.set()
                raise OSError("controlled close")
            return self.original.close()

        @property
        def closed(self):
            return self.original.closed

    def popen(*a, **kw):
        process = real_popen(*a, **kw)
        for operation in ["poll", "wait"]:
            original = getattr(process, operation)

            def call(*a, _op=operation, _real=original, **kw):
                if fault == _op and blocked.is_set():
                    reached.set()
                    raise OSError("controlled exit check")
                return _real(*a, **kw)

            setattr(process, operation, call)
        process.stdout, process.stderr = Stream(process.stdout), Stream(process.stderr)
        return process

    def sweep(*a, **kw):
        if fault == "sweep" and blocked.is_set():
            reached.set()
            return False
        return real_sweep(*a, **kw)

    monkeypatch.setattr(auxiliary.subprocess, "Popen", popen)
    monkeypatch.setattr(auxiliary, "terminate_process_tree", sweep)
    if fault == "owner":
        original = runner._execute

        def execute(record):
            original(record)
            reached.set()
            while blocked.is_set():
                time.sleep(0.005)

        monkeypatch.setattr(runner, "_execute", execute)
    thread, results, errors = capture_in_thread(runner, tmp_path, 'print("done")')
    try:
        assert reached.wait(2)
        record = runner._record
        assert record is not None
        assert not runner.close(deadline=time.monotonic())
        assert runner._record is record and not runner.retired
        assert not results
    finally:
        blocked.clear()
        finish(runner, thread)
    assert record.process.returncode is not None
    assert not record.thread.is_alive()
    assert all(
        s.closed and (s.thread is None or not s.thread.is_alive())
        for s in record.streams
    )


def test_blocked_consumer_does_not_block_owner_stop(tmp_path):
    from nz_coder.foundation.process_runner import ProcessInterrupted

    runner = make_auxiliary_runner(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def callback(line):
        entered.set()
        release.wait(3)
        return False

    thread, results, errors = capture_in_thread(
        runner,
        tmp_path,
        'import time; print("ready",flush=True); time.sleep(30)',
        on_line=callback,
    )
    try:
        assert entered.wait(2)
        record = runner._record
        runner.request_stop()
        assert runner.close(deadline=time.monotonic() + 2)
        assert thread.is_alive()
        assert record.process.returncode is not None and not record.thread.is_alive()
    finally:
        release.set()
        finish(runner, thread)
    assert not results and isinstance(errors[0], ProcessInterrupted)


def test_early_stop_keeps_stderr_accepted_before_handoff(tmp_path, monkeypatch):
    runner = make_auxiliary_runner(tmp_path)
    original = runner._drain

    def drain(record):
        if record.handoff.is_set():
            original(record)

    monkeypatch.setattr(runner, "_drain", drain)

    def consume(line):
        wait_until(lambda: not runner._record.streams[1].chunks.empty())
        return True

    try:
        result = runner.lines(
            argv(
                'import os,time; os.write(2,b"warning\\n"); print("ready",flush=True); time.sleep(30)'
            ),
            cwd=tmp_path,
            timeout=2,
            on_line=consume,
        )
        assert result.stopped_early and result.stderr == "warning"
    finally:
        finish(runner)


def test_lines_stderr_matches_original_decode_without_universal_newlines(tmp_path):
    from nz_coder.capabilities.ripgrep import _run_ripgrep_lines

    runner = make_auxiliary_runner(tmp_path)
    command = argv(
        "import os; os.write(1,b'ok\\n'); os.write(2,b'first\\rsecond\\r\\nthird:\\xff\\n')"
    )

    def consume(line):
        assert line == "ok"
        return False

    try:
        original = _run_ripgrep_lines(
            tmp_path,
            list(command),
            on_line=consume,
            cancel_event=None,
            timeout=2,
            reader_name="original-stderr-probe",
        )
        actual = _run_ripgrep_lines(
            tmp_path,
            list(command),
            on_line=consume,
            cancel_event=None,
            timeout=2,
            reader_name="runner-stderr-probe",
            runner=runner,
        )
        assert original.stderr == "first\rsecond\r\nthird:�"
        assert actual.stderr == original.stderr
        assert actual.code == original.code == 0
    finally:
        finish(runner)
