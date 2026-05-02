"""Error recovery: retry transient failures, inject diagnostics, avoid infinite loops."""

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

    def inject_diagnostic(self, messages: list, error_info: dict) -> None:
        """Add an error diagnostic message so the model can self-correct."""
        diag = (
            f"<error-diagnostic>\n"
            f"Error #{error_info['count']}: {error_info['error']}\n"
            f"The previous tool call failed. Please try a different approach.\n"
            f"</error-diagnostic>"
        )
        messages.append({"role": "user", "content": diag})
