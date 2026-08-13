"""High-level permission manager."""
from __future__ import annotations

from collections.abc import Callable

from nz_coder import config

from .checker import PermissionChecker
from .interaction import format_tool_summary, read_permission_answer
from .modes import normalize_mode
from .rules import PermissionRule, load_rules_from_settings, parse_rules


class PermissionManager:
    """Own permission mode, rules, and interactive approval."""

    def __init__(
        self,
        mode: str = None,
        renderer=None,
        asker: Callable[[str, dict], bool | str] | None = None,
    ):
        self._mode = normalize_mode(mode or config.PERMISSION_MODE)
        self._renderer = renderer
        self._asker = asker
        self._checker = PermissionChecker(self._mode)
        self._deny_rules: list[PermissionRule] = []
        self._allow_rules: list[PermissionRule] = []
        self._ask_rules: list[PermissionRule] = []
        self._special_allows: set[tuple[str, str]] = set()
        self._load_settings_rules()

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = normalize_mode(value)
        self._checker.mode = self._mode

    def _load_settings_rules(self) -> None:
        """Load permission rules from .nz-coder/settings.json if it exists."""
        allow_rules, deny_rules, ask_rules = load_rules_from_settings()
        self._allow_rules = allow_rules
        self._deny_rules = deny_rules
        self._ask_rules = ask_rules

    def add_allow(self, rule_str: str) -> None:
        """Dynamically add an allow rule for this session."""
        self._allow_rules.extend(parse_rules([rule_str], "allow"))

    def set_asker(self, asker: Callable[[str, dict], bool | str] | None) -> None:
        """Replace the interactive adapter without changing permission policy."""
        self._asker = asker

    def check(self, tool_name: str, tool_input: dict) -> dict:
        """Return {'behavior': 'allow'|'deny'|'ask', 'reason': str}."""
        return self._checker.check(
            tool_name=tool_name,
            tool_input=tool_input,
            allow_rules=self._allow_rules,
            deny_rules=self._deny_rules,
            ask_rules=self._ask_rules,
        )

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """Interactive confirmation. Returns True if user approves."""
        if self._asker is not None:
            answer = self._asker(tool_name, tool_input)
            if isinstance(answer, str):
                normalized = answer.strip().lower()
                if normalized == "always":
                    self._add_scoped_allow(tool_name, tool_input)
                    return True
                if normalized == "once":
                    return True
                if normalized == "reject":
                    return False
            return bool(answer)
        summary = format_tool_summary(tool_name, tool_input)
        if self._renderer:
            self._renderer.pause()
        try:
            answer = self._read_permission_answer(summary)
            if answer is None:
                return False
            if answer == "a":
                self.mode = "auto"
                return True
            if answer == "p":
                if tool_name == "bash":
                    cmd = tool_input.get("command", "")
                    prefix = cmd.split()[0] if cmd.split() else cmd
                    self.add_allow(f"bash(prefix:{prefix} )")
                    print(f"  [Permission] Added allow rule: bash(prefix:{prefix} )")
                return True
            if answer in ("n", "no"):
                return False
            return answer in ("y", "yes")
        finally:
            if self._renderer:
                self._renderer.resume()

    def ask_special(self, permission: str, metadata: dict) -> bool:
        """Ask for an InfCode-style runtime permission such as doom_loop.

        Special permissions are not ordinary tools and therefore bypass the
        tool rule checker. Without an injected interaction adapter they fail
        closed instead of trying to read a TTY from a workerless host.
        """
        tool = str(metadata.get("tool") or "*")
        key = (str(permission), tool)
        if key in self._special_allows:
            return True
        if self._asker is None:
            return False
        answer = self._asker(str(permission), dict(metadata))
        if isinstance(answer, str):
            normalized = answer.strip().lower()
            if normalized == "always":
                self._special_allows.add(key)
                return True
            return normalized == "once"
        return bool(answer)

    def _add_scoped_allow(self, tool_name: str, tool_input: dict) -> None:
        """Add the narrowest rule supported by the current rule grammar."""
        if tool_name == "bash":
            command = str(tool_input.get("command") or "").strip()
            prefix = command.split()[0] if command.split() else ""
            if prefix:
                self.add_allow(f"bash(prefix:{prefix} )")
                return
        self.add_allow(tool_name)

    def _read_permission_answer(self, summary: str) -> str | None:
        return read_permission_answer(
            summary=summary,
            renderer=self._renderer,
            tty_input=self._tty_input,
        )

    def _tty_input(self, prompt: str) -> str:
        with open("/dev/tty", "r+", encoding="utf-8", errors="replace") as tty:
            tty.write(prompt)
            tty.flush()
            line = tty.readline()
        if line == "":
            raise EOFError
        return line
