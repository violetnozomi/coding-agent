"""Durable SessionEvent journal behavior under queue pressure and close races."""
from __future__ import annotations

import json
import queue
import threading

from nz_coder.protocol.session_events import SessionEvent, _EventJournal


class _InertWorker:
    def join(self, timeout=None):
        return None

    def is_alive(self):
        return False


def _event(sequence: int, event_type: str = "message.part.delta") -> SessionEvent:
    return SessionEvent(
        type=event_type,
        properties={"index": sequence},
        sequence=sequence,
        timestamp=float(sequence),
        session_id="session-pressure",
        run_id="interaction-pressure",
        agent_id="agent-pressure",
        event_id=f"event-{sequence}",
    )


def _pressured_journal(tmp_path, *, ordinary: int = 2, reserve: int = 1):
    journal = _EventJournal(tmp_path / "events.jsonl", 4, "session-pressure")
    journal._ordinary_queue_limit = ordinary
    journal._critical_reserve = reserve
    journal._event_queue_limit = ordinary + reserve
    journal._queue = queue.Queue(maxsize=ordinary + reserve + 1)
    journal._worker = _InertWorker()
    return journal


def test_journal_accepted_event_is_never_evicted(tmp_path):
    journal = _pressured_journal(tmp_path)
    first = _event(1)
    second = _event(2)
    terminal = _event(3, "session.run.completed")

    assert journal.append(first) is True
    assert journal.append(second) is True
    assert journal.append(terminal) is True

    assert list(journal._queue.queue) == [first, second, terminal]


def test_journal_ordinary_capacity_excludes_critical_reserve(tmp_path):
    journal = _pressured_journal(tmp_path)

    assert journal.append(_event(1)) is True
    assert journal.append(_event(2)) is True
    assert journal.append(_event(3)) is False
    assert journal._queue.qsize() == journal._ordinary_queue_limit


def test_journal_critical_event_uses_reserved_capacity(tmp_path):
    journal = _pressured_journal(tmp_path)
    assert journal.append(_event(1)) is True
    assert journal.append(_event(2)) is True

    terminal = _event(3, "session.run.failed")

    assert journal.append(terminal) is True
    assert terminal in list(journal._queue.queue)


def test_journal_append_false_means_not_accepted(tmp_path):
    journal = _pressured_journal(tmp_path, ordinary=1, reserve=1)
    accepted = _event(1)
    rejected = _event(2)

    assert journal.append(accepted) is True
    assert journal.append(rejected) is False
    assert rejected not in list(journal._queue.queue)


def test_journal_sequence_has_no_gap_after_pressure(tmp_path):
    journal = _pressured_journal(tmp_path)
    assert journal.append(_event(1)) is True
    assert journal.append(_event(2)) is True
    assert journal.append(_event(3, "session.run.settled")) is True

    assert [item.sequence for item in journal._queue.queue] == [1, 2, 3]


def test_journal_handle_is_closed_only_by_writer_thread(tmp_path):
    close_threads: list[str] = []

    class RecordingJournal(_EventJournal):
        def _close_handle(self):
            if self._handle is not None:
                close_threads.append(threading.current_thread().name)
            return super()._close_handle()

    journal = RecordingJournal(
        tmp_path / "writer-owned.jsonl",
        4,
        "session-pressure",
    )
    assert journal.append(_event(1)) is True
    journal.close()

    assert close_threads
    assert all(name.startswith("nz-event-journal-") for name in close_threads)


def test_journal_join_timeout_reports_failure_without_cross_thread_close(tmp_path):
    class HungWorker:
        def join(self, timeout=None):
            return None

        def is_alive(self):
            return True

    class Handle:
        closed = False

        def close(self):
            self.closed = True

    journal = _pressured_journal(tmp_path)
    handle = Handle()
    journal._worker = HungWorker()
    journal._handle = handle

    journal.close()

    assert isinstance(journal.failure, RuntimeError)
    assert "did not close" in str(journal.failure)
    assert handle.closed is False


def test_journal_close_flushes_all_accepted_events(tmp_path):
    path = tmp_path / "flush.jsonl"
    journal = _EventJournal(path, 8, "session-pressure")
    accepted = [_event(1), _event(2), _event(3, "session.run.completed")]

    assert all(journal.append(event) for event in accepted)
    journal.close()

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [record["meta"]["sequence"] for record in records] == [1, 2, 3]
    assert journal.failure is None
