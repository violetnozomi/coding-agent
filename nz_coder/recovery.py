"""Error recovery: retry transient failures, inject diagnostics, avoid infinite loops."""
from __future__ import annotations


import re
import time
import traceback


class RecoveryState:
    def __init__(self):
        self.consecutive_errors = 0
        self.last_error = None
        self.max_retries = 3
        self.backoff_base = 2.0

    def record_success(self):
        self.consecutive_errors = 0
        self.last_error = None

    def record_error(self, error: Exception) -> dict:
        self.consecutive_errors += 1
        self.last_error = str(error)
        tb = traceback.format_exception(type(error), error, error.__traceback__)
        return {
            "count": self.consecutive_errors,
            "error": str(error),
            "traceback": "".join(tb[-3:]),
            "should_retry": self.consecutive_errors <= self.max_retries,
            "should_abort": self.consecutive_errors > self.max_retries,
        }

    def backoff_wait(self):
        if self.consecutive_errors <= 0:
            return
        wait = min(self.backoff_base ** self.consecutive_errors, 30)
        print(f"  [recovery] Waiting {wait:.0f}s before retry ({self.consecutive_errors}/{self.max_retries})...")
        time.sleep(wait)

    # FIXED: 删除 inject_diagnostic —— 该方法在 loop.py 中从未被调用（死代码），
    # 且与 loop.py 里已有的 _make_client_error_diag / tool_failure_diagnostic 职责重叠。
    # 保留死代码会在代码审查中引起混淆（"这里应该调用但没调用么？"）。

    def tool_failure_diagnostic(self, tool_name: str, output: str) -> str:
        """Return a targeted diagnostic for common coding-agent tool failures."""
        text = output or ""
        lower = text.lower()
        if "old_text not found" in lower:
            return (
                "<tool-failure-diagnostic>\n"
                f"Tool `{tool_name}` could not find the exact old_text.\n"
                "Do not retry from memory. Re-read the target file around the nearby context, "
                "copy the exact current snippet, then retry a smaller edit. For Python symbol-level "
                "changes, prefer `python_structural_edit` over repeated exact-text edits.\n"
                "</tool-failure-diagnostic>"
            )
        if "old_text matches" in lower:
            return (
                "<tool-failure-diagnostic>\n"
                f"Tool `{tool_name}` found multiple matches.\n"
                "Make the edit more specific by including more surrounding lines, or use a "
                "symbol-aware tool when editing Python code.\n"
                "</tool-failure-diagnostic>"
            )
        if text.startswith("Command exited with code"):
            return _build_test_failure_diagnostic(text)
        if text.startswith("Denied"):
            return (
                "<tool-failure-diagnostic>\n"
                f"Tool `{tool_name}` was denied by policy. Choose a safer, narrower command or "
                "use read/edit tools directly.\n"
                "</tool-failure-diagnostic>"
            )
        if text.startswith("Error:"):
            return (
                "<tool-failure-diagnostic>\n"
                f"Tool `{tool_name}` failed. Use the error output as ground truth, inspect the "
                "current workspace state, and try a different approach.\n"
                "</tool-failure-diagnostic>"
            )
        return ""

    def verification_gate_message(self, last_verification: dict | None) -> str:
        """Message injected when the model tries to finish without a passing check."""
        if last_verification:
            output = last_verification.get('output', '')
            excerpt = _extract_failure_excerpt(output)
            detail = (
                f"Last verification command: `{last_verification.get('command', 'unknown')}`\n"
                f"Last verification status: {last_verification.get('status', 'unknown')}\n"
                f"Relevant output:\n{excerpt}"
            )
        else:
            detail = "No verification command has passed since the last file edit."
        return (
            "<verification-required>\n"
            "You edited files but have not produced a passing verification after the edit. "
            "Do not finish yet. Run the most specific relevant test or import/behavior check, "
            "debug any failure, and only finish after a passing check.\n\n"
            f"{detail}\n"
            "</verification-required>"
        )


# ── Module-level output parsing helpers ──────────────────────────────────────

def _extract_failed_tests(output: str) -> list[str]:
    """Extract failing test IDs from pytest/unittest output."""
    tests: list[str] = []
    seen: set[str] = set()
    # pytest: "FAILED tests/foo.py::Bar::test_baz - AssertionError"
    for m in re.finditer(r"FAILED\s+([\w/\\.\-]+(?:::\w+)+)", output):
        t = m.group(1)
        if t not in seen:
            seen.add(t)
            tests.append(t)
    # unittest: "FAIL: test_baz (tests.test_foo.Bar)"
    for m in re.finditer(r"^(?:FAIL|ERROR):\s+(\w+)\s+\(([^)]+)\)", output, re.MULTILINE):
        t = f"{m.group(2)}.{m.group(1)}"
        if t not in seen:
            seen.add(t)
            tests.append(t)
    return tests


def _extract_regression_tests(output: str, baseline_passed: set[str] | None = None) -> list[str]:
    """Identify tests that were passing before but now fail (regressions).

    Without a baseline we can only detect tests explicitly marked as regressions
    in the output (e.g. SWE-bench PASS_TO_PASS failures). When a baseline set is
    provided, any test in both failed_tests and baseline_passed is a regression.
    """
    failed = set(_extract_failed_tests(output))
    if baseline_passed:
        return sorted(failed & baseline_passed)
    # Fallback: look for SWE-bench-style PASS_TO_PASS labels
    regressions: list[str] = []
    for m in re.finditer(r"PASS_TO_PASS.*?FAILED\s+([\w/\\.\-]+(?:::\w+)+)", output):
        regressions.append(m.group(1))
    return regressions


def _extract_traceback(output: str, max_chars: int = 1500) -> str:
    """Extract the most relevant traceback fragment from test output.

    Strategy:
    1. Find the last 'Traceback (most recent call last)' block — it's the root cause.
    2. If none, look for 'FAILURES' section header (pytest).
    3. Fall back to the tail of the output.
    """
    # Last traceback block
    tb_starts = [m.start() for m in re.finditer(r"Traceback \(most recent call last\)", output)]
    if tb_starts:
        start = tb_starts[-1]
        fragment = output[start:start + max_chars]
        return fragment.strip()

    # pytest FAILURES section
    failures_idx = output.find("FAILURES")
    if failures_idx >= 0:
        fragment = output[failures_idx:failures_idx + max_chars]
        return fragment.strip()

    # AssertionError / Error lines as last resort
    for marker in ("AssertionError", "Error:", "FAILED"):
        idx = output.rfind(marker)
        if idx >= 0:
            start = max(0, idx - 200)
            return output[start:idx + max_chars].strip()

    return output[-max_chars:].strip()


def _extract_failure_excerpt(output: str, max_chars: int = 2000) -> str:
    """Extract the most useful failure excerpt for gate messages.

    Locates the FAILURES section or last traceback rather than blindly
    truncating the tail (which often contains only summary lines).
    """
    if not output:
        return "(no output)"

    # pytest FAILURES section
    failures_idx = output.find("FAILURES")
    if failures_idx >= 0:
        # Include a bit of context before the marker
        start = max(0, failures_idx - 100)
        return output[start:start + max_chars].strip()

    # Last traceback
    tb_starts = [m.start() for m in re.finditer(r"Traceback \(most recent call last\)", output)]
    if tb_starts:
        start = max(0, tb_starts[-1] - 100)
        return output[start:start + max_chars].strip()

    # Fall back to tail
    return output[-max_chars:].strip()


def _build_test_failure_diagnostic(output: str) -> str:
    """Build a structured diagnostic from a failed bash command output."""
    failed_tests = _extract_failed_tests(output)
    traceback_text = _extract_traceback(output)
    regression_tests = _extract_regression_tests(output)

    parts = ["<test-failure-diagnostic>"]

    if regression_tests:
        parts.append(
            "⚠ Regressions detected (tests that were passing before):\n"
            + "\n".join(f"  - {t}" for t in regression_tests[:5])
        )
        parts.append(
            "Priority: FIRST restore these regressions, THEN fix the target failure. "
            "Do not sacrifice passing tests to fix a new one."
        )
    elif failed_tests:
        parts.append(
            "Failing tests:\n" + "\n".join(f"  - {t}" for t in failed_tests[:5])
        )

    if traceback_text:
        parts.append(f"Root cause:\n{traceback_text}")

    parts.append(
        "Next steps:\n"
        "1. Read the traceback above — identify the exact line and exception type.\n"
        "2. Inspect the implicated source file (not the test file) around that line.\n"
        "3. Make the minimal fix. Do not catch the exception; fix the root cause.\n"
        "4. Re-run the most specific failing test to confirm the fix."
    )
    parts.append("</test-failure-diagnostic>")
    return "\n\n".join(parts)
