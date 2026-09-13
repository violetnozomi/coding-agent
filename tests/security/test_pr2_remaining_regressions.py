"""Real-module regressions reconstructed from the PR #2 review (zip unavailable)."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from nz_coder.foundation import execution_identity as identity
from nz_coder.foundation.workspace_file_access import WorkspaceFileAccess
from nz_coder.runtime.process.workdir import scoped_workdir
from nz_coder.state.transaction import TransactionManager
from nz_coder.tools.files import write_files_batch


def node_command(tmp_path, monkeypatch, sizes):
    """Use a never-executed interpreter fixture; only payloads consume budget."""
    node = tmp_path / ("node.exe" if os.name == "nt" else "node")
    node.write_bytes(b"fixture-not-executable" * 8)
    monkeypatch.setattr(identity.shutil, "which", lambda _name: str(node))
    command = [str(node)]
    for index, size in enumerate(sizes):
        name = f"payload-{index}.js"
        (tmp_path / name).write_bytes(b"x" * size)
        if index + 1 < len(sizes):
            command.extend(("--require", name))
        else:
            command.append(name)
    return tuple(command)


def resolve(command, workspace):
    return identity.resolve_execution_identity(
        command, cwd=workspace, workspace=workspace,
        config_source="project", environment_profile="strict-service",
    )


def test_node_explicit_payloads_share_file_count_budget(tmp_path, monkeypatch):
    command = node_command(tmp_path, monkeypatch, [1, 1, 1])
    monkeypatch.setattr(identity, "_MAX_PAYLOAD_FILES", 2)
    with pytest.raises(identity.UnsafeExecutionIdentity, match="file budget"):
        resolve(command, tmp_path)


def test_node_explicit_payloads_share_total_byte_budget(tmp_path, monkeypatch):
    command = node_command(tmp_path, monkeypatch, [32, 32, 1])
    monkeypatch.setattr(identity, "_MAX_SINGLE_PAYLOAD_BYTES", 64)
    monkeypatch.setattr(identity, "_MAX_TOTAL_PAYLOAD_BYTES", 64)
    with pytest.raises(identity.UnsafeExecutionIdentity, match="byte budget"):
        resolve(command, tmp_path)


def inject_competing_publication(monkeypatch, target, callback=None):
    """Create the competitor inside the final publish syscall, after track."""
    original = os.open if os.name == "nt" else os.link
    hit = []

    def race(*args, **kwargs):
        destination = args[0] if os.name == "nt" else args[1]
        exclusive = os.name != "nt" or args[1] & os.O_EXCL
        if exclusive and Path(destination).name == target.name and not hit:
            hit.append(True)
            if callback is not None:
                callback()
            target.write_text("SENTINEL-COMPETITOR", encoding="utf-8")
        return original(*args, **kwargs)

    monkeypatch.setattr(os, "open" if os.name == "nt" else "link", race)
    return hit


def test_batch_no_replace_rollback_preserves_concurrent_creator(tmp_path, monkeypatch):
    target = tmp_path / "competitor.txt"
    hit = inject_competing_publication(monkeypatch, target)
    with scoped_workdir(tmp_path):
        result = write_files_batch([{"path": target.name, "content": "agent"}])
    assert hit, "the race must run at publication, after transaction preparation"
    assert result.startswith("Error:")
    assert target.exists(), "rollback must not delete a failed create's competitor"
    assert target.read_text(encoding="utf-8") == "SENTINEL-COMPETITOR"


def test_batch_rollback_restores_prior_member_and_preserves_competitor(tmp_path, monkeypatch):
    target = tmp_path / "competitor.txt"
    hit = inject_competing_publication(monkeypatch, target)
    with scoped_workdir(tmp_path):
        result = write_files_batch([
            {"path": "owned.txt", "content": "owned"},
            {"path": target.name, "content": "agent"},
        ])
    assert hit and result.startswith("Error:")
    assert not (tmp_path / "owned.txt").exists()
    assert target.read_text(encoding="utf-8") == "SENTINEL-COMPETITOR"


def test_failed_publication_does_not_grant_rollback_ownership(tmp_path, monkeypatch):
    transaction = TransactionManager()
    target = tmp_path / "competitor.txt"
    def prepared():
        record = next(iter(transaction._backups.values()))
        assert record.backup is None and not record.mutation.applied
    inject_competing_publication(monkeypatch, target, prepared)
    with scoped_workdir(tmp_path):
        transaction.begin()
        with pytest.raises(FileExistsError):
            WorkspaceFileAccess(tmp_path).write_text(
                target.name, "agent", transaction=transaction, overwrite=False,
            )
        transaction.rollback()
    assert transaction.state == "rolled_back"
    assert target.read_text(encoding="utf-8") == "SENTINEL-COMPETITOR"


@pytest.mark.parametrize("overwrite", [False] if os.name == "nt" else [False, True])
def test_exclusive_create_then_write_failure_remains_recoverable(tmp_path, monkeypatch, overwrite):
    """Windows fails after CREATE_NEW; POSIX after link/replace but before fsync."""
    transaction = TransactionManager()
    access = WorkspaceFileAccess(tmp_path)
    target = tmp_path / "owned.txt"
    original_fsync = os.fsync

    def fail_after_publication(fd):
        if target.exists():
            raise OSError("injected post-publication fsync failure")
        return original_fsync(fd)

    with scoped_workdir(tmp_path):
        transaction.begin()
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", fail_after_publication)
            with pytest.raises(OSError, match="post-publication"):
                access.write_text(target.name, "owned", transaction=transaction, overwrite=overwrite)
        assert target.exists()
        transaction.rollback()
    assert transaction.state == "rolled_back"
    assert not target.exists()


@pytest.mark.skipif(os.name != "nt", reason="CREATE_NEW creates target before write on Windows")
def test_windows_exclusive_create_write_raises_and_rolls_back(tmp_path, monkeypatch):
    transaction = TransactionManager()
    target = tmp_path / "owned.txt"
    original_fdopen = os.fdopen

    class FailingWriter:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def fileno(self):
            return self.stream.fileno()
        def write(self, data):
            self.stream.write(data[:1])
            raise OSError("injected write failure")

    def fdopen(fd, mode, *args, **kwargs):
        stream = original_fdopen(fd, mode, *args, **kwargs)
        return FailingWriter(stream) if mode == "wb" and target.exists() else stream

    with scoped_workdir(tmp_path):
        transaction.begin()
        with monkeypatch.context() as patch:
            patch.setattr(os, "fdopen", fdopen)
            with pytest.raises(OSError, match="write failure"):
                WorkspaceFileAccess(tmp_path).write_text(
                    target.name, "owned", transaction=transaction, overwrite=False,
                )
        transaction.rollback()
    assert not target.exists()
    assert transaction.state == "rolled_back"


def test_rollback_does_not_delete_replacement_of_owned_target(tmp_path):
    transaction = TransactionManager()
    access = WorkspaceFileAccess(tmp_path)
    target = tmp_path / "owned.txt"
    parked = tmp_path / "parked.txt"
    with scoped_workdir(tmp_path):
        transaction.begin()
        access.write_text(target.name, "owned", transaction=transaction, overwrite=False)
        target.rename(parked)
        target.write_text("SENTINEL-REPLACEMENT", encoding="utf-8")
        transaction.rollback()
        assert transaction.state == "rollback_partial"
        assert target.read_text(encoding="utf-8") == "SENTINEL-REPLACEMENT"
        transaction.rollback()
        assert target.read_text(encoding="utf-8") == "SENTINEL-REPLACEMENT"
        target.unlink()
        parked.rename(target)
        transaction.rollback()
        assert not target.exists()
        target.write_text("later-external", encoding="utf-8")
        assert transaction.rollback() == ""
    assert target.read_text(encoding="utf-8") == "later-external"


def test_failed_second_mutation_keeps_first_mutation_receipt(tmp_path, monkeypatch):
    transaction = TransactionManager()
    access = WorkspaceFileAccess(tmp_path)
    with scoped_workdir(tmp_path):
        transaction.begin()
        access.write_text("owned.txt", "first", transaction=transaction)
        with monkeypatch.context() as patch:
            patch.setattr(os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError("publish failed")))
            with pytest.raises(OSError, match="publish failed"):
                access.write_text("owned.txt", "second", transaction=transaction)
        transaction.rollback()
    assert not (tmp_path / "owned.txt").exists()


def test_prepared_existing_file_does_not_restore_external_modification(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("original", encoding="utf-8")
    transaction = TransactionManager()
    with scoped_workdir(tmp_path):
        transaction.begin()
        transaction.track(target.name)
        target.write_text("SENTINEL-EXTERNAL", encoding="utf-8")
        transaction.rollback()
    assert target.read_text(encoding="utf-8") == "SENTINEL-EXTERNAL"


def test_prior_existing_member_restores_after_exclusive_conflict(tmp_path, monkeypatch):
    transaction = TransactionManager()
    access = WorkspaceFileAccess(tmp_path)
    first = tmp_path / "first.txt"
    first.write_text("original", encoding="utf-8")
    os.utime(first, ns=(1700000000000000000, 1700000000000000000))
    before = first.stat()
    target = tmp_path / "competitor.txt"
    inject_competing_publication(monkeypatch, target)
    with scoped_workdir(tmp_path):
        transaction.begin()
        access.write_text(first.name, "modified", transaction=transaction)
        with pytest.raises(FileExistsError):
            access.write_text(target.name, "agent", transaction=transaction, overwrite=False)
        transaction.rollback()
    assert first.read_text(encoding="utf-8") == "original"
    # Preserve the existing POSIX metadata guarantee; Windows' pre-existing
    # backup-rename recovery does not restore timestamps (recorded in struct.md).
    if os.name != "nt":
        assert first.stat().st_mtime_ns == before.st_mtime_ns
        assert first.stat().st_mode == before.st_mode
    assert target.read_text(encoding="utf-8") == "SENTINEL-COMPETITOR"


@pytest.mark.skipif(os.name != "nt", reason="Windows fdopen follows successful CREATE_NEW")
def test_windows_fdopen_failure_keeps_create_receipt(tmp_path, monkeypatch):
    target = tmp_path / "owned.txt"
    transaction = TransactionManager()
    original = os.fdopen

    def fail_fdopen(fd, *args, **kwargs):
        if target.exists():
            raise OSError("fdopen failed")
        return original(fd, *args, **kwargs)

    with scoped_workdir(tmp_path):
        transaction.begin()
        with monkeypatch.context() as patch:
            patch.setattr(os, "fdopen", fail_fdopen)
            with pytest.raises(OSError, match="fdopen failed"):
                WorkspaceFileAccess(tmp_path).write_text(
                    target.name, "owned", transaction=transaction, overwrite=False,
                )
        transaction.rollback()
    assert transaction.state == "rolled_back"
    assert not target.exists()


@pytest.mark.parametrize("flag", ["--require", "--loader", "--import"])
@pytest.mark.parametrize("sizes,accepted", [([32, 32], True), ([32, 33], False)])
def test_main_and_hooks_share_exact_byte_boundary(tmp_path, monkeypatch, flag, sizes, accepted):
    command = list(node_command(tmp_path, monkeypatch, sizes))
    command[1] = flag
    monkeypatch.setattr(identity, "_MAX_SINGLE_PAYLOAD_BYTES", 64)
    monkeypatch.setattr(identity, "_MAX_TOTAL_PAYLOAD_BYTES", 64)
    monkeypatch.setattr(identity, "_MAX_PAYLOAD_FILES", 2)
    if accepted:
        assert resolve(command, tmp_path).fingerprint
    else:
        with pytest.raises(identity.UnsafeExecutionIdentity, match="byte budget"):
            resolve(command, tmp_path)


def test_payload_single_file_limit_still_applies(tmp_path, monkeypatch):
    command = node_command(tmp_path, monkeypatch, [65])
    monkeypatch.setattr(identity, "_MAX_SINGLE_PAYLOAD_BYTES", 64)
    with pytest.raises(identity.UnsafeExecutionIdentity, match="file budget"):
        resolve(command, tmp_path)


def test_payload_budget_is_independent_after_failure_and_between_threads(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    command = node_command(tmp_path, monkeypatch, [8, 8, 8])
    monkeypatch.setattr(identity, "_MAX_PAYLOAD_FILES", 2)
    with pytest.raises(identity.UnsafeExecutionIdentity):
        resolve(command, tmp_path)
    valid = (command[0], "--require", "payload-0.js", "payload-1.js")
    barrier = Barrier(2)
    original = Path.open

    def open_together(path, *args, **kwargs):
        if path.name == "payload-0.js":
            barrier.wait(timeout=5)
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", open_together)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: resolve(valid, tmp_path).fingerprint, range(2)))
    assert results[0] == results[1] == resolve(valid, tmp_path).fingerprint


def test_duplicate_payload_is_charged_per_reference_and_affects_fingerprint(tmp_path, monkeypatch):
    command = node_command(tmp_path, monkeypatch, [8])
    duplicate = (command[0], "--require", "payload-0.js", "payload-0.js")
    monkeypatch.setattr(identity, "_MAX_PAYLOAD_FILES", 2)
    monkeypatch.setattr(identity, "_MAX_TOTAL_PAYLOAD_BYTES", 16)
    assert resolve(command, tmp_path).fingerprint != resolve(duplicate, tmp_path).fingerprint
    monkeypatch.setattr(identity, "_MAX_TOTAL_PAYLOAD_BYTES", 15)
    with pytest.raises(identity.UnsafeExecutionIdentity, match="byte budget"):
        resolve(duplicate, tmp_path)
    monkeypatch.setattr(identity, "_MAX_TOTAL_PAYLOAD_BYTES", 16)
    monkeypatch.setattr(identity, "_MAX_PAYLOAD_FILES", 1)
    with pytest.raises(identity.UnsafeExecutionIdentity, match="file budget"):
        resolve(duplicate, tmp_path)


@pytest.mark.parametrize("limit", ["files", "bytes"])
def test_budget_rejection_stops_opening_following_payloads(tmp_path, monkeypatch, limit):
    command = node_command(tmp_path, monkeypatch, [8, 8, 8])
    monkeypatch.setattr(identity, "_MAX_PAYLOAD_FILES", 1 if limit == "files" else 5)
    monkeypatch.setattr(identity, "_MAX_TOTAL_PAYLOAD_BYTES", 8 if limit == "bytes" else 64)
    opened = []
    original = Path.open

    def observe(path, *args, **kwargs):
        if path.suffix == ".js":
            opened.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", observe)
    with pytest.raises(identity.UnsafeExecutionIdentity):
        resolve(command, tmp_path)
    assert opened == ["payload-0.js"]


@pytest.mark.parametrize("limit", ["single", "total"])
def test_growing_payload_is_limited_by_actual_bytes(tmp_path, monkeypatch, limit):
    command = node_command(tmp_path, monkeypatch, [1])
    monkeypatch.setattr(identity, "_MAX_SINGLE_PAYLOAD_BYTES", 8 if limit == "single" else 64)
    monkeypatch.setattr(identity, "_MAX_TOTAL_PAYLOAD_BYTES", 8 if limit == "total" else 64)
    original = Path.open
    read_bytes = []

    class CountingReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def read(self, size):
            chunk = self.stream.read(size)
            read_bytes.append(len(chunk))
            return chunk

    def grow_after_precheck(path, *args, **kwargs):
        if path.suffix == ".js":
            with original(path, "wb") as output:
                output.write(b"x" * 128)
            return CountingReader(original(path, *args, **kwargs))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", grow_after_precheck)
    with pytest.raises(identity.UnsafeExecutionIdentity, match="budget"):
        resolve(command, tmp_path)
    assert sum(read_bytes) == 9  # At most one byte past the bound to detect growth.


def test_package_files_use_the_resolution_budget(tmp_path, monkeypatch):
    executable = tmp_path / ("python.exe" if os.name == "nt" else "python")
    executable.write_bytes(b"interpreter" * 100)
    package = tmp_path / "package"
    package.mkdir()
    (package / "__main__.py").write_bytes(b"x" * 32)
    (package / "helper.py").write_bytes(b"x" * 32)
    monkeypatch.setattr(identity, "_MAX_PAYLOAD_FILES", 2)
    monkeypatch.setattr(identity, "_MAX_SINGLE_PAYLOAD_BYTES", 64)
    monkeypatch.setattr(identity, "_MAX_TOTAL_PAYLOAD_BYTES", 64)
    command = (str(executable), "-m", "package")
    assert resolve(command, tmp_path).fingerprint
    (package / "helper.py").write_bytes(b"x" * 33)
    with pytest.raises(identity.UnsafeExecutionIdentity, match="byte budget"):
        resolve(command, tmp_path)
    (package / "helper.py").write_bytes(b"x" * 32)
    (package / "third.py").write_bytes(b"")
    with pytest.raises(identity.UnsafeExecutionIdentity, match="file budget"):
        resolve(command, tmp_path)
