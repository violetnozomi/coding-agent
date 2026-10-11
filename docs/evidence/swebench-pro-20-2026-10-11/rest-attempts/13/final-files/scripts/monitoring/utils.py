import contextlib
import fnmatch
import os
import subprocess

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_SUBMITTED,
    JobEvent,
)
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.util import undefined


def _job_id(func, id):
    # Default the job id to the function name to avoid duplicating it everywhere
    return id or func.__name__


class _OlSchedulerMixin:
    """Shared behaviour for Open Library schedulers.

    Jobs added through this scheduler default their id to the wrapped function's
    name so they can be retrieved with ``scheduler.get_job("<function name>")``.
    """

    def __init__(self):
        super().__init__({'apscheduler.timezone': 'UTC'})
        self.add_listener(
            job_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_SUBMITTED
        )

    def add_job(
        self,
        func,
        trigger=None,
        args=None,
        kwargs=None,
        id=None,
        name=None,
        misfire_grace_time=undefined,
        coalesce=undefined,
        max_instances=undefined,
        next_run_time=undefined,
        jobstore="default",
        executor="default",
        replace_existing=False,
        **trigger_args,
    ):
        return super().add_job(
            func,
            trigger,
            args,
            kwargs,
            _job_id(func, id),
            name,
            misfire_grace_time,
            coalesce,
            max_instances,
            next_run_time,
            jobstore,
            executor,
            replace_existing,
            **trigger_args,
        )


class OlBlockingScheduler(_OlSchedulerMixin, BlockingScheduler):
    """A blocking scheduler that defaults job ids to the function name."""


class OlAsyncIOScheduler(_OlSchedulerMixin, AsyncIOScheduler):
    """An asyncio-based scheduler that defaults job ids to the function name."""


def job_listener(event: JobEvent):
    if event.code == EVENT_JOB_SUBMITTED:
        print(f"Job {event.job_id} has started.", flush=True)
    elif event.code == EVENT_JOB_EXECUTED:
        print(f"Job {event.job_id} completed successfully.", flush=True)
    elif event.code == EVENT_JOB_ERROR:
        print(f"Job {event.job_id} failed.", flush=True)


def bash_run(cmd: str, sources: list[str] | None = None, capture_output=False):
    if not sources:
        sources = []

    source_paths = [
        (
            os.path.join("scripts", "monitoring", source)
            if not os.path.isabs(source)
            else source
        )
        for source in sources
    ]
    bash_command = "\n".join(
        (
            'set -e',
            *(f'source "{path}"' for path in source_paths),
            cmd,
        )
    )

    return subprocess.run(
        [
            "bash",
            "-c",
            bash_command,
        ],
        check=True,
        # Mainly for testing:
        capture_output=capture_output,
        text=capture_output if capture_output else None,
    )


def limit_server(allowed_hosts: list[str], scheduler: BaseScheduler):
    """
    Decorator that only keeps a job registered on allowed hosts.

    The current host is read from the ``HOSTNAME`` environment variable (rather
    than via socket lookups so it can be controlled in tests/deploys). Jobs are
    registered as usual via ``scheduler.scheduled_job(...)``; if the current host
    does not match any of the allowed patterns the job is un-registered again.

    :param allowed_hosts: List of allowed hosts. Supports exact names and simple
        prefix wildcards, eg "ol-web*" or "allowed-server*". A short host also
        matches its fully qualified domain name, eg "ol-web0" matches
        "ol-web0.us.archive.org".
    :param scheduler: The scheduler the job was registered with.
    """

    def decorator(func):
        hostname = os.environ.get("HOSTNAME")
        assert hostname

        # Compare both the full hostname and its short (un-qualified) form so a
        # pattern like "ol-web0" matches "ol-web0.us.archive.org".
        candidates = {hostname, hostname.split(".")[0]}
        if not any(
            fnmatch.fnmatch(host, pattern)
            for host in candidates
            for pattern in allowed_hosts
        ):
            with contextlib.suppress(JobLookupError):
                # The job was never registered (eg limit_server used on its own).
                scheduler.remove_job(_job_id(func, None))
        return func

    return decorator
