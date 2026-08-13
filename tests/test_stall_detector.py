"""Contract tests translated from InfCodeX FEATURE_178 stall detector."""
from __future__ import annotations


def test_three_identical_calls_in_window_emit_stall_envelope():
    from nz_coder.runtime.stall_detector import StallDetector

    detector = StallDetector(disabled=False)
    assert detector.record_tool_use("read", {"path": "a.py"}).kind == "no_stall"
    assert detector.record_tool_use("read", {"path": "a.py"}).kind == "no_stall"
    signal = detector.record_tool_use("read", {"path": "a.py"})

    assert signal.kind == "stall"
    assert signal.occurrence_count == 3
    assert signal.cache_hit_count == 0
    assert signal.turns == (1, 2, 3)
    assert signal.envelope == (
        '[Stall detector signal]\n'
        'tool=read input={"path":"a.py"} '
        'occurrence_count=3 cache_hit_count=0 turns=[1,2,3]'
    )


def test_identical_calls_can_be_interleaved_inside_window():
    from nz_coder.runtime.stall_detector import StallDetector

    detector = StallDetector(disabled=False)
    detector.record_tool_use("read", {"path": "a.py"})
    detector.record_tool_use("grep", {"pattern": "x"})
    detector.record_tool_use("read", {"path": "a.py"})
    signal = detector.record_tool_use("read", {"path": "a.py"})

    assert signal.kind == "stall"
    assert signal.turns == (1, 3, 4)


def test_cache_hit_lowers_threshold_to_two_calls():
    from nz_coder.runtime.stall_detector import StallDetector

    detector = StallDetector(disabled=False)
    detector.record_tool_use("read", {"path": "a.py"}, cache_hit=True)
    signal = detector.record_tool_use("read", {"path": "a.py"})

    assert signal.kind == "stall"
    assert signal.cache_hit_count == 1


def test_stable_input_order_window_eviction_and_reset(monkeypatch):
    from nz_coder.runtime.stall_detector import StallDetector

    detector = StallDetector(window_size=4, disabled=False)
    detector.record_tool_use("read", {"path": "a.py", "offset": 1})
    detector.record_tool_use("grep", {"pattern": "x"})
    detector.record_tool_use("grep", {"pattern": "y"})
    detector.record_tool_use("grep", {"pattern": "z"})
    detector.record_tool_use("grep", {"pattern": "w"})
    assert detector.record_tool_use("read", {"offset": 1, "path": "a.py"}).kind == "no_stall"
    detector.reset()
    assert detector.size() == 0

    monkeypatch.setenv("KODAX_STALL_DETECT", "0")
    disabled = StallDetector()
    for _ in range(3):
        signal = disabled.record_tool_use("read", {"path": "a.py"})
    assert signal.kind == "no_stall"
    assert disabled.size() == 0
