"""隔离评测控制端。用 python -m nz_coder.evaluation.isolated_permissions HOST_MANIFEST 监看一轮。"""
from __future__ import annotations

import argparse
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

from nz_coder.http_service.client import NZCoderClient, NZCoderHTTPError
from nz_coder.tool_platform.permissioning.auto_router import canonical_action_digest


def validate_container(info, container_id, image_id, workspace, relay_socket, image_env=()):
    """只接受控制方事先固定的容器、镜像和单个可写项目目录。"""
    host = info["HostConfig"]
    if (not re.fullmatch(r"[a-f0-9]{64}", container_id) or info["Id"] != container_id
            or not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id) or info["Image"] != image_id
            or not info["State"]["Running"] or host["NetworkMode"] != "none"
            or not host["ReadonlyRootfs"] or host["Privileged"] or host.get("CapAdd")
            or not re.fullmatch(r"[1-9]\d*(?::[1-9]\d*)?", info["Config"]["User"])
            or "ALL" not in host["CapDrop"] or not set(host["SecurityOpt"] or ()).intersection(
                {"no-new-privileges", "no-new-privileges=true"})
            or host.get("PidMode") or host.get("IpcMode") not in (None, "", "private")
            or host.get("Devices") or host.get("DeviceRequests") or host.get("VolumesFrom")
            or set(host.get("Tmpfs") or {}) - {"/tmp"}):
        raise ValueError("Evaluation container does not match the isolated run")
    # 继承审定镜像的运行时配置；评测控制凭据和模型真密钥只留在宿主。
    base_names = {"PATH", "LANG", "LC_ALL", "TZ", "GPG_KEY", "PYTHON_VERSION", "PYTHON_SHA256",
                  "PYTHON_PIP_VERSION", "PYTHON_SETUPTOOLS_VERSION", "PYTHON_GET_PIP_URL", "PYTHON_GET_PIP_SHA256"}
    allowed = {entry for entry in image_env if entry.partition("=")[0] in base_names}
    allowed.update({"HOME=/tmp/home", "XDG_STATE_HOME=/tmp/state", "XDG_CACHE_HOME=/tmp/cache",
                    "XDG_CONFIG_HOME=/tmp/config", "PYTHONDONTWRITEBYTECODE=1"})
    if relay_socket:
        allowed.update({"API_KEY=evaluation-local-only", "OPENAI_API_KEY=evaluation-local-only",
                        "NZ_MEMORY_EVAL_SOCKET=/model/provider.sock",
                        "API_BASE_URL=http://memory-evaluation.invalid/v1"})
    if not set(info["Config"].get("Env") or ()).issubset(allowed):
        raise ValueError("Unconfirmed environment in evaluation container")
    expected = {"/work": (str(workspace.resolve(strict=True)), True)}
    if relay_socket:
        relay = Path(relay_socket).resolve(strict=True)
        if not relay.is_socket() or relay.is_relative_to(workspace):
            raise ValueError("Expected a dedicated external model relay socket")
        expected["/model/provider.sock"] = (str(relay), False)
    observed = {}
    for mount in info["Mounts"]:
        if mount["Type"] == "tmpfs" and mount["Destination"] == "/tmp":
            continue
        if mount["Type"] != "bind" or mount.get("Propagation") != "rprivate":
            raise ValueError("Unexpected evaluation mount")
        observed[mount["Destination"]] = (mount["Source"], mount["RW"])
    if observed != expected:
        raise ValueError("Evaluation workspace or control mounts differ")


def _approval_marker(token, session_id, run_id, request_id):
    # 回执随服务端事件持久化。重新连入时也能认出本次评测的批准。
    identity = json.dumps([session_id, run_id, request_id]).encode()
    return "nz-evaluation-once-" + hmac.digest(token.encode(), identity, "sha256").hex()


def _reply_once(client, session_id, run_id, workspace, allowed, request_id, marker):
    from nz_coder.tools.bash import _resolve_bash_workdir

    request = next((r for r in client.pending_permissions(session_id) if r["id"] == request_id), None)
    if (request is None or request["kind"] != "permission" or request["session_id"] != session_id
            or request["permission"] != "bash" or time.time() >= request["expires_at"]):
        return False
    info = client.get_session(session_id)
    if (info["id"] != session_id or info["workspace"] != str(workspace)
            or not info["running"] or info["active_interaction_run_id"] != run_id):
        return False
    args = request["tool_input"]
    cwd, error = _resolve_bash_workdir(args.get("workdir"), workspace=workspace)
    if error or (canonical_action_digest("bash", args, workspace), str(cwd)) not in allowed:
        return False
    try:
        return client.reply_permission(session_id, request_id, "once", message=marker)
    except NZCoderHTTPError as exc:
        if exc.code != "interaction_not_found":
            raise
        return False


def permission_wait_seconds(intervals):
    end, total = float("-inf"), 0.0
    for start, stop in sorted(intervals):
        total += max(0.0, stop - max(start, end))
        end = max(end, stop)
    return total


def monitor_run(client, packet, workspace, emit):
    sid, run = packet["session_id"], packet["run_id"]
    info = client.get_session(sid)
    if (not run or not packet["start_cursor"] or info["id"] != sid or info["workspace"] != str(workspace)
            or info["active_interaction_run_id"] != run):
        raise ValueError("Manifest does not identify the expected session and run")
    allowed = {(canonical_action_digest(item["tool"], item["args"], workspace), item["cwd"])
               for item in packet["operations"] if item["tool"] == "bash"}
    starts, ends, automatic, external = {}, {}, set(), set()
    kinds = ["session.run.started", "permission.asked", "permission.replied", "session.run.settled"]
    stream = client.events(sid, last_event_id=packet["start_cursor"], event_types=kinds, reconnect_attempts=1)
    started = False
    stopped_at_heartbeat = False
    try:
        for event in stream:
            kind = event["type"]
            if kind == "server.connected":
                continue
            if kind == "server.event_gap":
                raise ValueError("Gap in permission event audit")
            if kind == "server.heartbeat":
                state = client.get_session(sid)
                if not state["running"] or state["active_interaction_run_id"] != run:
                    # 心跳后可能刚好结算，不能越过正在入队的结算事件。
                    if stopped_at_heartbeat:
                        raise ValueError("Run settled without a replayable settlement event")
                    stopped_at_heartbeat = True
                continue
            meta, data = event["meta"], event["properties"]
            if meta.get("session_id") != sid or meta.get("interaction_run_id") != run:
                continue
            if kind == "session.run.started":
                started = True
            elif not started:
                raise ValueError("Run-start event missing from approval audit")
            elif kind == "permission.asked":
                key = data["id"]
                if key in starts:
                    continue
                starts[key] = data["created_at"]
                marker = _approval_marker(packet["token"], sid, run, key)
                answered = _reply_once(client, sid, run, workspace, allowed, key, marker)
                emit({"request_id": key, "event": "automatic_once" if answered else "manual_or_already_resolved"})
            elif kind == "permission.replied":
                key = data["request_id"]
                ends[key] = meta["timestamp"]
                if data.get("reply") == "once" and data.get("message") == _approval_marker(packet["token"], sid, run, key):
                    automatic.add(key)
                elif not data.get("reason"):
                    external.add(key)
            elif kind == "session.run.settled":
                break
        else:
            raise ValueError("Permission event stream ended before run settlement")
    finally:
        if hasattr(stream, "close"):
            stream.close()
    if starts.keys() != ends.keys() or any(ends[key] < first for key, first in starts.items()):
        raise ValueError("Incomplete permission event intervals")
    return {"permission_requests": len(starts), "automatically_approved": len(automatic),
            "external_replies": len(external), "evaluator_clicks": None if external else 0,
            "user_clicks": None if external else 0,
            "permission_wait_seconds": permission_wait_seconds((first, ends[key]) for key, first in starts.items()),
            "automatic_wait_seconds": permission_wait_seconds((starts[key], ends[key]) for key in automatic)}


def _monitor():
    packet = json.load(sys.stdin)
    client = NZCoderClient(f"http://127.0.0.1:{int(packet['port'])}", packet["token"], timeout=30)
    emit = lambda row: print(json.dumps(row), flush=True)
    emit(monitor_run(client, packet, Path("/work"), emit))


def main():
    parser = argparse.ArgumentParser(description="Reply once to exact preauthorized operations in one isolated evaluation run.")
    parser.add_argument("manifest", type=Path, help="Operator-owned private JSON outside the container mounts")
    args = parser.parse_args()
    path = args.manifest.resolve(strict=True)
    if path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        parser.error("Use an owner-private manifest")
    config = json.loads(path.read_text())
    workspace = Path(config["host_workspace"]).resolve(strict=True)
    if path.is_relative_to(workspace):
        parser.error("The control manifest must be outside the mounted project")
    cid = config["container_id"]
    if not re.fullmatch(r"[a-f0-9]{64}", cid):
        parser.error("Expected full container ID")
    info, = json.loads(subprocess.check_output(["docker", "inspect", "--type", "container", cid], timeout=10))
    pinned_image = config["image_id"]
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", pinned_image):
        parser.error("Expected approved image digest")
    image, = json.loads(subprocess.check_output(["docker", "image", "inspect", pinned_image], timeout=10))
    validate_container(info, cid, pinned_image, workspace, config.get("relay_socket"), image["Config"].get("Env") or ())
    packet = {key: config[key] for key in ("port", "token", "session_id", "run_id", "start_cursor", "operations")}
    subprocess.run(["docker", "exec", "-i", "-w", "/usr/local", cid, "/usr/local/bin/python", "-I", "-c",
                    "from nz_coder.evaluation.isolated_permissions import _monitor; _monitor()"],
                   input=json.dumps(packet), text=True, check=True)


if __name__ == "__main__":
    main()
