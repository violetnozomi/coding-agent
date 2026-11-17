"""Model/host path boundary regression tests."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("path", [
    ".gitignore", ".gitattributes", ".gitmodules", ".gitkeep", ".git-backup",
    ".github/workflows/check.yml", ".hgignore", ".ssh-config", ".aws_notes",
    ".azure-pipelines.yml", ".nz-coder-notes.md", ".nz-coder-runs-notes.md",
    ".gcloudignore", ".kubeconfig.sample", ".env.example", ".env.sample", ".env.template",
    ".git notes.txt", ".ssh backup/config", ".git;notes", ".git|notes",
])
@pytest.mark.parametrize("interpreter", [False, True])
def test_shell_private_scan_preserves_complete_public_name(tmp_path, path, interpreter):
    import shlex
    from nz_coder.foundation.workspace_paths import model_command_private_path

    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("public fixture\n")
    command = ("python -c " + shlex.quote(f"from pathlib import Path; print(Path({path!r}).read_text())")
               if interpreter else "cat " + shlex.quote(path))
    assert model_command_private_path(command, tmp_path, command_cwd=tmp_path) is None


@pytest.mark.parametrize("path", [
    ".git/config", ".git", ".hg/store/data", ".nz-coder/sessions/private.json",
    ".nz-coder-runs/private.json", ".ssh/id_ed25519", ".aws/credentials",
    ".azure/token", ".gcloud/token", ".kube/config", ".env.production.local",
    ".env.example.private",
    ".git/folder with space/config", ".git/author's-notes", ".ssh/my key.pem",
])
@pytest.mark.parametrize("interpreter", [False, True])
def test_shell_private_scan_keeps_real_protected_paths_blocked(tmp_path, path, interpreter):
    import shlex
    from nz_coder.foundation.workspace_paths import model_command_private_path

    command = ("python -c " + shlex.quote(f"from pathlib import Path; print(Path({path!r}).read_text())")
               if interpreter else "cat " + shlex.quote(path))
    assert model_command_private_path(command, tmp_path, command_cwd=tmp_path) is not None


def test_original_gitignore_command_passes_policy_and_executes(tmp_path):
    from nz_coder.permissions import PermissionManager
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.bash import run_bash

    (tmp_path / ".gitignore").write_text("*.pyc\n")
    command = "cat .gitignore 2>/dev/null; echo '---'; python3 --version"
    with scoped_workdir(tmp_path):
        decision = PermissionManager("auto", workspace_trusted=True).check("bash", {"command": command})
        assert decision["behavior"] != "deny", decision
        output = run_bash(command)
    assert "*.pyc" in output
    assert "Python 3" in output
    assert output.execution_facts[0].exit_code == 0


@pytest.mark.parametrize("command,blocked", [
    ("cat .git*/config", True),
    ("cat .[g]it/config", True),
    ("cat .ssh*/id_ed25519", True),
    ("cat .gitignore*", False),
    ("cat .gitattributes?", False),
    ("cat .github/*", False),
])
def test_shell_patterns_distinguish_private_directories_from_public_prefixes(tmp_path, command, blocked):
    from nz_coder.foundation.workspace_paths import model_command_private_path

    assert (model_command_private_path(command, tmp_path, command_cwd=tmp_path) is not None) is blocked


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        ".env.local",
        "nested/.env.production",
        ".git/config",
        ".git\\config",
        "nested/.git/HEAD",
        ".nz-coder/sessions/private.json",
        "nested/.nz-coder/runs/trace.jsonl",
        ".ssh/id_ed25519",
        "known_hosts",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".aws/credentials",
        "service-account.json",
        "private.pem",
        "private.key",
        "identity.p12",
    ],
)
def test_model_read_rejects_private_and_credential_paths(tmp_path, path):
    from nz_coder.foundation.workspace_paths import WorkspacePathError, WorkspacePathPolicy

    policy = WorkspacePathPolicy(tmp_path)

    with pytest.raises(WorkspacePathError, match="Model access blocked"):
        policy.validate_model_read(path)


@pytest.mark.parametrize("path", [".env.example", ".env.sample", ".env.template", "src/app.py"])
def test_public_templates_and_source_paths_remain_model_readable(tmp_path, path):
    from nz_coder.foundation.workspace_paths import WorkspacePathPolicy

    target = tmp_path / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("safe\n", encoding="utf-8")

    assert WorkspacePathPolicy(tmp_path).validate_model_read(path) == target.resolve()


def test_internal_access_is_distinct_from_model_access(tmp_path):
    from nz_coder.foundation.workspace_paths import WorkspacePathError, WorkspacePathPolicy

    private = tmp_path / ".nz-coder" / "sessions" / "state.json"
    private.parent.mkdir(parents=True)
    private.write_text("{}", encoding="utf-8")
    policy = WorkspacePathPolicy(tmp_path)

    assert policy.validate_internal_access(private) == private.resolve()
    with pytest.raises(WorkspacePathError):
        policy.validate_model_read(private)


def test_symlink_escape_and_nonexistent_descendant_are_rejected(tmp_path):
    from nz_coder.foundation.workspace_paths import WorkspacePathError, WorkspacePathPolicy

    workspace = tmp_path / "repo"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    link = workspace / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable")
    policy = WorkspacePathPolicy(workspace)

    with pytest.raises(WorkspacePathError, match="escapes workspace"):
        policy.validate_model_read("link/secret.txt")
    with pytest.raises(WorkspacePathError, match="escapes workspace"):
        policy.validate_model_write("link/future/new.txt")


def test_model_tools_cannot_read_list_write_or_search_private_paths(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import list_directory, read_file, write_file
    from nz_coder.tools.search import glob_search, grep_search

    (tmp_path / ".env").write_text("API_KEY=sentinel-path-secret\n", encoding="utf-8")
    private = tmp_path / "nested" / ".git"
    private.mkdir(parents=True)
    (private / "config").write_text("sentinel-path-secret\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        assert read_file(".env").startswith("Error: Model access blocked")
        assert list_directory("nested/.git").startswith("Error: Model access blocked")
        assert write_file("nested/.env.local", "bad").startswith("Error: Model access blocked")
        grep_output = grep_search("sentinel-path-secret", ".")
        glob_output = glob_search("**/.git/**", ".")
        assert "sentinel-path-secret" not in grep_output
        assert "config" not in glob_output


def test_every_file_mutation_entrypoint_rejects_private_paths(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import (
        apply_patch,
        edit_file,
        replace_lines,
        write_file,
        write_files_batch,
    )

    (tmp_path / ".env").write_text("SAFE=before\n", encoding="utf-8")
    with scoped_workdir(tmp_path):
        results = [
            write_file(".env", "SAFE=after\n"),
            edit_file(".env", "before", "after"),
            replace_lines(".env", 1, 1, "SAFE=after"),
            write_files_batch([{"path": ".env", "content": "SAFE=after\n"}], overwrite=True),
            apply_patch([{"op": "replace", "path": ".env", "old_text": "before", "new_text": "after"}]),
        ]

    assert all(result.startswith("Error:") and "blocked" in result for result in results)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "SAFE=before\n"


def test_shell_and_process_block_known_private_path_reads(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.bash import run_bash
    from nz_coder.tools.process import run_process

    (tmp_path / ".env").write_text("API_KEY=sentinel-path-secret\n", encoding="utf-8")
    with scoped_workdir(tmp_path):
        bash_result = run_bash("cat .env")
        process_result = run_process("start", command="cat .env", tty=False)

    assert bash_result.startswith("Error:") and "Model access blocked" in bash_result
    assert process_result.startswith("Error:") and "Model access blocked" in process_result
    assert "sentinel-path-secret" not in bash_result + process_result


def test_model_listing_filters_nested_private_children(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import list_directory

    (tmp_path / "pkg" / ".nz-coder").mkdir(parents=True)
    (tmp_path / "pkg" / ".nz-coder" / "secret.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pkg" / "public.py").write_text("pass\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        output = list_directory("pkg", depth=3)

    assert "public.py" in output
    assert ".nz-coder" not in output
    assert "secret.json" not in output


def test_read_directory_filters_private_child_names(tmp_path):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tools.files import read_file

    (tmp_path / ".git").mkdir()
    (tmp_path / ".nz-coder").mkdir()
    (tmp_path / ".env").write_text("API_KEY=secret", encoding="utf-8")
    (tmp_path / "public.py").write_text("pass\n", encoding="utf-8")

    with scoped_workdir(tmp_path):
        output = read_file(".")

    assert "public.py" in output
    assert ".git" not in output
    assert ".nz-coder" not in output
    assert ".env" not in output


def test_untrusted_workspace_shell_requires_confirmation_even_in_auto_mode(
    tmp_path, monkeypatch
):
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning.manager import PermissionManager

    monkeypatch.setenv(
        "NZ_CODER_WORKSPACE_TRUST_STORE", str(tmp_path.parent / "trust.json")
    )
    with scoped_workdir(tmp_path):
        manager = PermissionManager("auto", workspace_trusted=False)

    assert manager.check("bash", {"command": "ls"})["behavior"] == "ask"
    assert manager.check(
        "process", {"operation": "start", "command": "python server.py"}
    )["behavior"] == "ask"


def test_exactly_trusted_workspace_retains_auto_shell_behavior(tmp_path, monkeypatch):
    from nz_coder.foundation.workspace_trust import WorkspaceTrustStore, load_config_snapshot
    from nz_coder.runtime.process.workdir import scoped_workdir
    from nz_coder.tool_platform.permissioning.manager import PermissionManager

    trust_path = tmp_path.parent / "trusted.json"
    monkeypatch.setenv("NZ_CODER_WORKSPACE_TRUST_STORE", str(trust_path))
    snapshot = load_config_snapshot(tmp_path)
    WorkspaceTrustStore(trust_path).trust(
        tmp_path, "workspace-config", snapshot.workspace_fingerprint
    )

    with scoped_workdir(tmp_path):
        manager = PermissionManager("auto", workspace_trusted=True)

    assert manager.check("bash", {"command": "ls"})["behavior"] == "allow"
