"""在真实Bash的Python子进程里检查无敏感哨兵与容器边界。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket


def main():
    config = json.loads(Path("probe-config.json").read_text())
    checks = {}
    local = Path("local-probe.tmp")
    local.write_bytes(b"workspace-writable")
    checks["workspace_read_write"] = local.read_bytes() == b"workspace-writable"
    local.unlink()
    targets = {**config["outside_targets"], "parent": "../private/sentinel.txt", "symlink": "outside-link",
               "proc_init_root": "/proc/1/root" + config["outside_targets"]["absolute"],
               "proc_host_parent": f'/proc/{config["host_pid"]}/root' + config["outside_targets"]["absolute"]}
    for name, path in targets.items():
        try:
            Path(path).read_bytes()
        except (OSError, ValueError):
            checks["denied_" + name] = True
        else:
            checks["denied_" + name] = False
        try:
            Path(path).write_bytes(b"nonsensitive escape probe")
        except OSError:
            checks["write_denied_" + name] = True
        else:
            checks["write_denied_" + name] = False
    try:
        with Path("/runtime/nz_coder/__init__.py").open("ab") as handle:
            handle.write(b"forbidden")
    except OSError:
        checks["runtime_readonly"] = True
    else:
        checks["runtime_readonly"] = False
    checks["repository_and_git_absent"] = not any(Path(p).exists() for p in
        ("/runtime/.git", "/runtime/docs", "/runtime/tests", "/runtime/nz_coder/evaluation"))
    checks["docker_socket_absent"] = not Path("/var/run/docker.sock").exists()
    checks["no_real_credentials_in_environment"] = not any(k in os.environ for k in
        ("OPENAI_API_KEY", "DEEPSEEK_API_KEY", "API_KEY", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"))
    links = []
    for directory in Path("/proc").glob("[0-9]*/fd"):
        try:
            for descriptor in directory.iterdir():
                try:
                    links.append(os.readlink(descriptor))
                except OSError:
                    pass
        except OSError:
            pass
    checks["no_inherited_external_sentinel_fd"] = not any(config["outside_targets"]["absolute"] in link for link in links)
    for name, address in (("host_loopback", ("127.0.0.1", config["host_port"])),
                          ("nonlocal_testnet", ("198.51.100.1", 9))):
        with socket.socket() as channel:
            channel.settimeout(0.3)
            try:
                channel.connect(address)
            except OSError:
                checks["network_denied_" + name] = True
            else:
                checks["network_denied_" + name] = False
    with socket.socket(socket.AF_UNIX) as channel:
        channel.connect("/model/main.sock")
        checks["necessary_unix_channel_available"] = True
    print(json.dumps({"checks": checks, "passed": all(checks.values()),
                      "process": {"pid": os.getpid(), "parent_pid": os.getppid(),
                                  "pid_namespace": os.readlink("/proc/self/ns/pid"),
                                  "network_namespace": os.readlink("/proc/self/ns/net"),
                                  "ipc_namespace": os.readlink("/proc/self/ns/ipc")}}))
    raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    main()
