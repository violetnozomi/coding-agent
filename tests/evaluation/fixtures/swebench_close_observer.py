"""只读记录真实 SWE 关闭边界；不替换工具、资源或队列的实际行为。"""
from __future__ import annotations

import faulthandler
import functools
import json
import os
from pathlib import Path
import threading
import time


def record(event, **facts):
    row = dict(event=event, monotonic=time.monotonic(), timestamp=time.time(),
               pid=os.getpid(), thread=threading.current_thread().name, **facts)
    descriptor = os.open("/result/lifecycle.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(descriptor, (json.dumps(row) + "\n").encode())
    finally:
        os.close(descriptor)


def install():
    from multiprocessing.queues import Queue
    from nz_coder.runtime.execution.loop import ProductRunEnvironment
    from nz_coder.intelligence import service
    from nz_coder.lsp import manager
    from nz_coder.swebench import orchestrator

    stack_output = Path("/result/thread-stacks.txt").open("a")
    original_close = ProductRunEnvironment.close

    @functools.wraps(original_close)
    def close(self):
        record("cleanup_started")
        faulthandler.dump_traceback_later(45, file=stack_output)
        try:
            return original_close(self)
        finally:
            faulthandler.cancel_dump_traceback_later()
            record("cleanup_finished", failures=self.environment_cleanup_failures,
                   complete=self.environment_cleanup_complete,
                   threads=[dict(name=t.name, daemon=t.daemon) for t in threading.enumerate()])

    ProductRunEnvironment.close = close

    def observe(target, attribute, label):
        original = getattr(target, attribute)

        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            record("resource_close_started", resource=label)
            try:
                result = original(*args, **kwargs)
            except BaseException as exc:
                record("resource_close_failed", resource=label, error=repr(exc))
                raise
            record("resource_close_finished", resource=label)
            return result

        setattr(target, attribute, wrapped)

    for name in ("_close_environment_run_controls", "_close_environment_stall_sidecar",
                 "_close_environment_background_agents", "_close_repo_intelligence",
                 "_close_environment_mcp", "_close_environment_object",
                 "_close_environment_provider_runtimes", "_close_environment_event_bus"):
        observe(ProductRunEnvironment, name, name)
    observe(service, "release_repo_intelligence", "release_repo_intelligence")
    observe(service.RepoIntelligenceService, "close", "repo_service")
    observe(manager, "close_workspace_clients", "workspace_lsp")
    observe(orchestrator, "_close_attempt_environment", "attempt_environment")

    original_run = orchestrator._run_cancellable_attempt

    @functools.wraps(original_run)
    async def run(*args, **kwargs):
        result = await original_run(*args, **kwargs)
        record("agent_execution_finished", agent_status=result)
        return result

    orchestrator._run_cancellable_attempt = run
    original_put, original_get = Queue.put, Queue.get

    def put(self, obj, *args, **kwargs):
        result = original_put(self, obj, *args, **kwargs)
        if isinstance(obj, dict) and "tool_events" in obj:
            record("worker_result_enqueued", ok=obj.get("ok"))
        return result

    def get(self, *args, **kwargs):
        result = original_get(self, *args, **kwargs)
        if isinstance(result, dict) and "tool_events" in result:
            record("worker_result_received", ok=result.get("ok"))
        return result

    Queue.put, Queue.get = put, get
    original_attempt = orchestrator._run_agent_attempt_in_subprocess

    @functools.wraps(original_attempt)
    def attempt(*args, **kwargs):
        result = original_attempt(*args, **kwargs)
        from multiprocessing import active_children
        record("attempt_parent_returned", agent_status=result["status"],
               children=[child.pid for child in active_children()])
        return result

    orchestrator._run_agent_attempt_in_subprocess = attempt
