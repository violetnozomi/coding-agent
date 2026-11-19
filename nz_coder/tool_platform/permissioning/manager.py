"""High-level permission manager."""
from __future__ import annotations

from collections.abc import Callable
import copy
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from nz_coder.foundation import config

from .checker import PermissionChecker
from .interaction import PermissionInteractionError, format_tool_summary, normalize_permission_reply, read_permission_answer
from .modes import normalize_mode
from .rules import (
    PermissionRule,
    first_matching_rule,
    load_rules_from_bytes,
    load_rules_from_settings,
    parse_rules,
    persist_allow_rule,
    scoped_allow_rule,
)

if TYPE_CHECKING:
    from nz_coder.foundation.project_control import ProjectControlSnapshot


log = logging.getLogger(__name__)


class PermissionManager:
    """Own permission mode, rules, and interactive approval."""

    def __init__(
        self,
        mode: str = None,
        renderer=None,
        asker: Callable[[str, dict], bool | str] | None = None,
        workspace_trusted: bool = True,
        project_control_snapshot: ProjectControlSnapshot | None = None,
    ):
        from nz_coder.runtime.process.workdir import current_workdir
        self._permission_workspace = current_workdir().resolve()
        self._command_fork = False
        self._mode = normalize_mode(mode or config.PERMISSION_MODE)
        self._renderer = renderer
        self._asker = asker
        if project_control_snapshot is not None:
            effective_trust = bool(project_control_snapshot.trusted)
        else:
            # A legacy boolean may preserve non-project behavior only when no
            # settings file exists. It can never authorize current disk rules.
            try:
                from nz_coder.runtime.process.workdir import current_workdir

                has_project_settings = (
                    current_workdir() / ".nz-coder" / "settings.json"
                ).exists()
            except OSError:
                has_project_settings = True
            effective_trust = bool(workspace_trusted and not has_project_settings)
        self._checker = PermissionChecker(
            self._mode,
            workspace_trusted=effective_trust,
        )
        self._workspace_trusted = bool(
            project_control_snapshot is not None
            and project_control_snapshot.trusted
        )
        self._legacy_restrictive_rules = bool(
            project_control_snapshot is None and not workspace_trusted
        )
        self._project_control_snapshot = project_control_snapshot
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
        """Load Project rules only from the immutable run-owned snapshot."""
        settings = (
            self._project_control_snapshot.get(".nz-coder/settings.json")
            if self._project_control_snapshot is not None
            else None
        )
        if settings is not None:
            allow_rules, deny_rules, ask_rules = load_rules_from_bytes(settings.content)
        elif self._legacy_restrictive_rules:
            _ignored_allow, deny_rules, ask_rules = load_rules_from_settings()
            allow_rules = []
        else:
            allow_rules, deny_rules, ask_rules = [], [], []
        try:
            from nz_coder.runtime.process.workdir import current_workdir
            from .grants import UserGrantStore

            user_allow = parse_rules(
                UserGrantStore().load(current_workdir()), "allow"
            )
        except (OSError, PermissionError, ValueError):
            user_allow = []
        # A repository-owned allow rule is authority.  Until the exact
        # workspace control plane is trusted, only restrictive project rules
        # may influence execution.
        self._allow_rules = [
            *user_allow,
            *(allow_rules if self._workspace_trusted else []),
        ]
        self._deny_rules = deny_rules
        self._ask_rules = ask_rules

    def add_allow(self, rule_str: str) -> None:
        """Dynamically add an allow rule for this session."""
        self._allow_rules.extend(parse_rules([rule_str], "allow"))

    def fork_for_commands(self, workspace: Path) -> PermissionManager:
        """冻结父权限；精确输入允许不能随委派迁移到另一个实际工作区。"""
        fork = copy.copy(self)
        root = Path(workspace).resolve()
        fork._checker = PermissionChecker(self.mode, workspace_trusted=self._checker.workspace_trusted)
        fork._allow_rules = [rule for rule in self._allow_rules
                             if root == self._permission_workspace or not rule.content.startswith("input-sha256:")]
        fork._deny_rules = list(self._deny_rules)
        fork._ask_rules = list(self._ask_rules)
        fork._special_allows = set()
        fork._renderer = None
        fork._permission_workspace = root
        fork._command_fork = True
        return fork

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

    def explicit_rule_behavior(
        self,
        tool_name: str,
        tool_input: dict,
    ) -> str | None:
        """Return the first matching configured rule behavior, if any."""
        for behavior, rules in (
            ("deny", self._deny_rules),
            ("allow", self._allow_rules),
            ("ask", self._ask_rules),
        ):
            if first_matching_rule(rules, tool_name, tool_input) is not None:
                return behavior
        return None

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """Interactive confirmation. Returns True if user approves."""
        if self._command_fork and self._asker is None:
            raise PermissionInteractionError("unavailable")
        if self._asker is not None:
            try:
                displayed_input = dict(tool_input)
                if self._command_fork:
                    from nz_coder.tools.bash import _resolve_bash_workdir
                    cwd, error = _resolve_bash_workdir(tool_input.get("workdir"), workspace=self._permission_workspace)
                    if error:
                        raise PermissionInteractionError("unavailable")
                    displayed_input["workdir"] = str(cwd)
                answer = normalize_permission_reply(self._asker(tool_name, displayed_input))
            except PermissionInteractionError:
                raise
            except Exception:
                raise PermissionInteractionError("unavailable") from None
            if answer == "always":
                self._add_scoped_allow(tool_name, tool_input, persist=True)
            return answer != "reject"
        summary = format_tool_summary(tool_name, tool_input)
        if self._renderer:
            self._renderer.pause()
        try:
            answer = self._read_permission_answer(summary)
            if answer is None:
                raise PermissionInteractionError("unavailable")
            if answer == "a":
                self.mode = "auto"
                return True
            if answer == "p":
                persisted = self._add_scoped_allow(
                    tool_name,
                    tool_input,
                    persist=True,
                )
                if persisted:
                    print("  [Permission] Added persistent scoped allow rule", file=sys.stderr)
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
            raise PermissionInteractionError("unavailable")
        try:
            answer = normalize_permission_reply(self._asker(str(permission), dict(metadata)))
        except PermissionInteractionError:
            raise
        except Exception:
            raise PermissionInteractionError("unavailable") from None
        if answer == "always":
            self._special_allows.add(key)
        return answer != "reject"

    def _add_scoped_allow(
        self,
        tool_name: str,
        tool_input: dict,
        *,
        persist: bool = False,
    ) -> bool:
        """Add a narrow rule, optionally persisting it for future sessions."""
        try:
            rule = scoped_allow_rule(tool_name, tool_input)
            if persist:
                from nz_coder.runtime.process.workdir import scoped_workdir
                with scoped_workdir(self._permission_workspace):
                    persist_allow_rule(rule)
        except (OSError, PermissionError, ValueError) as exc:
            log.warning("Could not persist permission rule: %s", exc)
            return False
        if rule not in self._allow_rules:
            self._allow_rules.append(rule)
        return True

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
