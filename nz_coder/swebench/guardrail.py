"""Patch risk guardrail: pure static analysis of a unified diff.

PatchGuardrail.analyze(patch) → PatchRiskReport

No LLM calls, no agent knowledge, no orchestration decisions.
The guardrail only flags; the orchestrator decides what to do with the report.
"""
from __future__ import annotations

import re
from pathlib import Path

from nz_coder.swebench.models import PatchRiskItem, PatchRiskReport


_RISKY_METHOD_NAMES: frozenset[str] = frozenset(
    {"read", "write", "process_lines", "get_lines", "write_header"}
)

_DJANGO_RISKY_FILES: tuple[str, ...] = (
    "django/conf/",
    "django/core/",
    "django/http/",
    "django/urls/",
)

_ENUM_COERCION_FILES: frozenset[str] = frozenset({
    "django/db/models/fields/__init__.py",
    "django/db/models/query_utils.py",
})


class PatchGuardrail:
    """Stateless patch risk analyser.

    Usage::

        guardrail = PatchGuardrail()
        report = guardrail.analyze(patch_text, regression_context=has_regressions)
        if report.has_blocking:
            # don't apply previous patch
            ...
        result["risk_reasons"] = report.risk_labels()
    """

    def analyze(self, patch: str, *, regression_context: bool = False) -> PatchRiskReport:
        """Analyse *patch* and return a PatchRiskReport.

        Args:
            patch: unified diff string.
            regression_context: True when official PASS_TO_PASS regressions
                exist — several checks are escalated to "blocking" severity.
        """
        items: list[PatchRiskItem] = []
        items.extend(self._check_deleted_methods(patch, regression_context))
        items.extend(self._check_deleted_classes(patch, regression_context))
        items.extend(self._check_added_classes(patch, regression_context))
        items.extend(self._check_risky_added_methods(patch, regression_context))
        items.extend(self._check_magic_separator(patch, regression_context))
        items.extend(self._check_broad_except(patch, regression_context))
        items.extend(self._check_case_insensitive(patch, regression_context))
        items.extend(self._check_django_import_cycle(patch, regression_context))
        items.extend(self._check_test_file_changes(patch))
        items.extend(self._check_enum_coercion(patch, regression_context))
        return PatchRiskReport(items=items)

    # ── Individual check methods ──────────────────────────────────────────────

    def _check_deleted_methods(self, patch: str, regression_context: bool) -> list[PatchRiskItem]:
        deleted = self._parse_deleted_methods_raw(patch)
        if not deleted:
            return []
        label = "deleted_methods_under_regression_guard" if regression_context else "deleted_methods"
        severity = "blocking" if regression_context else "warning"
        detail = _fmt_symbol_detail(deleted)
        return [PatchRiskItem(category=label, detail=detail, severity=severity)]

    def _check_deleted_classes(self, patch: str, regression_context: bool) -> list[PatchRiskItem]:
        deleted = self._parse_deleted_classes_raw(patch)
        if not deleted:
            return []
        label = "deleted_classes_under_regression_guard" if regression_context else "deleted_classes"
        severity = "blocking" if regression_context else "warning"
        return [PatchRiskItem(category=label, detail=_fmt_symbol_detail(deleted), severity=severity)]

    def _check_added_classes(self, patch: str, regression_context: bool) -> list[PatchRiskItem]:
        if not regression_context:
            return []
        added = self._parse_added_classes_raw(patch)
        if not added:
            return []
        return [PatchRiskItem(
            category="added_classes_under_regression_guard",
            detail=_fmt_symbol_detail(added),
            severity="blocking",
        )]

    def _check_risky_added_methods(self, patch: str, regression_context: bool) -> list[PatchRiskItem]:
        if not regression_context:
            return []
        risky = self._parse_risky_added_methods_raw(patch)
        if not risky:
            return []
        return [PatchRiskItem(
            category="added_methods_under_regression_guard",
            detail=_fmt_symbol_detail(risky),
            severity="blocking",
        )]

    def _check_magic_separator(self, patch: str, regression_context: bool) -> list[PatchRiskItem]:
        if not self._raw_has_magic_separator(patch):
            return []
        label = "magic_separator_index_under_header_rows" if regression_context else "magic_separator_index"
        severity = "blocking" if regression_context else "warning"
        return [PatchRiskItem(category=label, detail="", severity=severity)]

    def _check_broad_except(self, patch: str, regression_context: bool) -> list[PatchRiskItem]:
        if not self._raw_has_broad_except(patch):
            return []
        label = "broad_except_under_regression_guard" if regression_context else "broad_except"
        severity = "blocking" if regression_context else "warning"
        return [PatchRiskItem(category=label, detail="", severity=severity)]

    def _check_case_insensitive(self, patch: str, regression_context: bool) -> list[PatchRiskItem]:
        if not self._raw_has_case_insensitive_no_norm(patch):
            return []
        label = (
            "case_insensitive_match_without_token_normalization_under_regression_guard"
            if regression_context
            else "case_insensitive_match_without_token_normalization"
        )
        severity = "blocking" if regression_context else "warning"
        return [PatchRiskItem(category=label, detail="", severity=severity)]

    def _check_django_import_cycle(self, patch: str, regression_context: bool) -> list[PatchRiskItem]:
        if not self._raw_has_top_level_django_urls_import(patch):
            return []
        label = "top_level_django_urls_import_under_regression_guard" if regression_context else "top_level_django_urls_import"
        severity = "blocking" if regression_context else "warning"
        return [PatchRiskItem(category=label, detail="", severity=severity)]

    def _check_test_file_changes(self, patch: str) -> list[PatchRiskItem]:
        if not self._raw_has_test_file_changes(patch):
            return []
        return [PatchRiskItem(category="tests_modified", detail="", severity="warning")]

    def _check_enum_coercion(self, patch: str, regression_context: bool) -> list[PatchRiskItem]:
        if not self._raw_has_broad_enum_coercion(patch):
            return []
        label = "broad_enum_value_coercion_under_regression_guard" if regression_context else "broad_enum_value_coercion"
        severity = "blocking"  # always blocking — this breaks TextChoices semantics
        return [PatchRiskItem(category=label, detail="", severity=severity)]

    # ── Raw parse / detect primitives ────────────────────────────────────────
    # These are the low-level functions migrated from swebench_lite.py.
    # They are semi-public (single underscore) so models.py can call them for
    # culprit hints without importing the full guardrail output path.

    def _parse_deleted_methods_raw(self, patch: str) -> dict[str, list[str]]:
        deleted, added = self._parse_method_changes(patch)

        def _is_renamed_public(name: str, added_names: list[str]) -> bool:
            stripped = name.rsplit(".", 1)[-1].lstrip("_")
            return any(a.rsplit(".", 1)[-1].lstrip("_") == stripped for a in added_names)

        result = {
            path: [
                name.rsplit(".", 1)[-1]
                for name in names
                if name not in added.get(path, []) and not _is_renamed_public(name, added.get(path, []))
            ]
            for path, names in deleted.items()
        }
        return {path: names for path, names in result.items() if names}

    def _parse_risky_added_methods_raw(self, patch: str) -> dict[str, list[str]]:
        _, added = self._parse_method_changes(patch)
        deleted, _ = self._parse_method_changes(patch)
        net_added = {
            path: [name for name in names if name not in deleted.get(path, [])]
            for path, names in added.items()
        }
        result: dict[str, list[str]] = {}
        for filepath, methods in net_added.items():
            risky = [m for m in methods if m.rsplit(".", 1)[-1] in _RISKY_METHOD_NAMES]
            if risky:
                result[filepath] = risky
        return result

    def _parse_deleted_classes_raw(self, patch: str) -> dict[str, list[str]]:
        deleted, added = self._parse_class_changes(patch)
        result = {
            path: [name for name in names if name not in added.get(path, [])]
            for path, names in deleted.items()
        }
        return {path: names for path, names in result.items() if names}

    def _parse_added_classes_raw(self, patch: str) -> dict[str, list[str]]:
        deleted, added = self._parse_class_changes(patch)
        result = {
            path: [name for name in names if name not in deleted.get(path, [])]
            for path, names in added.items()
        }
        return {path: names for path, names in result.items() if names}

    @staticmethod
    def _parse_method_changes(patch: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        deleted: dict[str, list[str]] = {}
        added: dict[str, list[str]] = {}
        current_file: str | None = None
        current_class: str | None = None
        for line in patch.splitlines():
            if line.startswith("+++ ") and "/dev/null" not in line:
                raw = line[4:]
                current_file = raw[2:] if raw.startswith("b/") else raw
                current_class = None
                deleted.setdefault(current_file, [])
                added.setdefault(current_file, [])
                continue
            if current_file is None or line.startswith(("+++", "---")):
                continue
            marker = line[:1]
            if marker not in {" ", "+", "-"}:
                continue
            text = line[1:]
            class_match = re.match(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\b", text.strip())
            if class_match:
                current_class = class_match.group(1)
                continue
            method_match = re.match(r"(?:async\s+)?def\s+(\w+)\s*\(", text.strip())
            if not method_match:
                continue
            name = method_match.group(1)
            qualified = f"{current_class}.{name}" if current_class else name
            target = deleted if marker == "-" else added if marker == "+" else None
            if target is not None and qualified not in target[current_file]:
                target[current_file].append(qualified)
        return deleted, added

    @staticmethod
    def _parse_class_changes(patch: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
        deleted: dict[str, list[str]] = {}
        added: dict[str, list[str]] = {}
        current_file: str | None = None
        for line in patch.splitlines():
            if line.startswith("+++ ") and "/dev/null" not in line:
                raw = line[4:]
                current_file = raw[2:] if raw.startswith("b/") else raw
                deleted.setdefault(current_file, [])
                added.setdefault(current_file, [])
            elif current_file and line.startswith("-") and not line.startswith("---"):
                match = re.match(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\b", line[1:].strip())
                if match and match.group(1) not in deleted[current_file]:
                    deleted[current_file].append(match.group(1))
            elif current_file and line.startswith("+") and not line.startswith("+++"):
                match = re.match(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\b", line[1:].strip())
                if match and match.group(1) not in added[current_file]:
                    added[current_file].append(match.group(1))
        return deleted, added

    def _raw_has_magic_separator(self, patch: str) -> bool:
        if "header_rows" not in patch:
            return False
        for line in patch.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            text = line[1:]
            if "lines = [" in text and re.search(r"lines\[[01]\]", text):
                return True
        return False

    def _raw_has_broad_except(self, patch: str) -> bool:
        for line in patch.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            text = line[1:].strip()
            if re.match(r"except\s*(?:(?:Exception|BaseException)(?:\s+as\s+\w+)?)?\s*:", text):
                return True
        return False

    def _raw_has_case_insensitive_no_norm(self, patch: str) -> bool:
        adds_case_insensitive = False
        adds_normalization = False
        for line in patch.splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            text = line[1:]
            if "re.IGNORECASE" in text or re.search(r"\bre\.I\b", text):
                adds_case_insensitive = True
            if re.search(r"\.\s*(?:upper|lower|casefold)\s*\(", text):
                adds_normalization = True
        return adds_case_insensitive and not adds_normalization

    def _raw_has_top_level_django_urls_import(self, patch: str) -> bool:
        current_file: str | None = None
        for line in patch.splitlines():
            if line.startswith("+++ ") and "/dev/null" not in line:
                raw = line[4:]
                current_file = raw[2:] if raw.startswith("b/") else raw
                continue
            if not current_file or not current_file.startswith(_DJANGO_RISKY_FILES):
                continue
            if not line.startswith("+") or line.startswith("+++"):
                continue
            text = line[1:]
            if text.startswith(("from django.urls import ", "from django.urls.", "import django.urls")):
                return True
        return False

    def _raw_has_test_file_changes(self, patch: str) -> bool:
        for line in patch.splitlines():
            if not line.startswith("+++ ") or "/dev/null" in line:
                continue
            raw = line[4:]
            path = raw[2:] if raw.startswith("b/") else raw
            parts = Path(path).parts
            if "tests" in parts or path.startswith("tests/"):
                return True
        return False

    def _raw_has_broad_enum_coercion(self, patch: str) -> bool:
        current_file = ""
        added_lines: list[str] = []
        for line in patch.splitlines():
            if line.startswith("+++ ") and "/dev/null" not in line:
                raw = line[4:]
                current_file = raw[2:] if raw.startswith("b/") else raw
                continue
            if not current_file or not line.startswith("+") or line.startswith("+++"):
                continue
            if current_file not in _ENUM_COERCION_FILES:
                continue
            added_lines.append(line[1:].strip())
        text = "\n".join(added_lines)
        return (
            "isinstance(value, enum.Enum)" in text
            or "def __set__(self, instance, value):" in text
        )


# ── Module-level helpers ──────────────────────────────────────────────────────

def _fmt_symbol_detail(symbols_by_file: dict[str, list[str]]) -> str:
    parts = []
    for path, symbols in sorted(symbols_by_file.items()):
        parts.append(f"{Path(path).name}:{','.join(symbols[:5])}")
    return ";".join(parts)
