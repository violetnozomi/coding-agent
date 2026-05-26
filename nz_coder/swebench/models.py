"""Core data structures for SWE-bench Lite integration.

FailureFeedback  — structured output from the official harness (adapter → agent)
PatchRiskItem    — a single risk finding from guardrail analysis
PatchRiskReport  — aggregated guardrail output (guardrail → orchestrator)
RetryPlan        — orchestrator's decision for one retry instance
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# ── FailureFeedback ───────────────────────────────────────────────────────────

@dataclass
class FailureFeedback:
    """Structured output from the official SWE-bench Docker harness.

    Produced by SWEBenchAdapter.load_feedback().
    Consumed by RetryOrchestrator (decision making) and serialised to an
    agent-readable prompt block via to_agent_prompt().
    """
    instance_id: str
    resolved: bool | str            # False / True / "unknown"
    patch_applied: bool | str       # False / True / "unknown"
    fail_to_pass: list[str]         # FAIL_TO_PASS tests that still fail
    pass_to_pass: list[str]         # PASS_TO_PASS tests now regressing
    passing_tests: list[str]        # tests that passed in this official run
    output_excerpt: str             # relevant slice of test_output.txt

    @property
    def has_regressions(self) -> bool:
        return bool(self.pass_to_pass)

    # ── Agent prompt serialisation ────────────────────────────────────────────

    def to_agent_prompt(self, previous_patch: str = "") -> str:
        """Serialise to an XML block injected into the agent conversation.

        Replaces the old _format_official_failure_feedback() function.
        """
        failing = _fmt_bullets(self.fail_to_pass) if self.fail_to_pass else "- unknown"
        regressions = _fmt_bullets(self.pass_to_pass) if self.pass_to_pass else "- none recorded"
        passing_sample = _fmt_bullets(self.passing_tests[:12]) if self.passing_tests else "- none recorded"
        constraints = self._retry_constraints(previous_patch)
        excerpt = self.output_excerpt or "(no test_output.txt excerpt available)"

        return (
            "<official-swebench-feedback>\n"
            f"Instance: {self.instance_id}\n"
            f"Official resolved: {self.resolved}\n"
            f"Patch successfully applied: {self.patch_applied}\n"
            "This feedback comes from the official Docker SWE-bench harness, so treat it "
            "as stronger evidence than local source-only checks.\n\n"
            "Failing tests:\n"
            f"{failing}\n\n"
            "Regression failures:\n"
            f"{regressions}\n\n"
            "Passing tests from the same official run:\n"
            f"{passing_sample}\n\n"
            f"{constraints}\n\n"
            "Relevant official test output excerpt:\n"
            f"{excerpt}\n\n"
            "Required next step: explain the behavioral mismatch to yourself from this "
            "traceback, inspect the implicated reader/writer code, create or update a "
            "non-empty minimal source patch instead of stopping, and run the narrowest "
            "verification command that "
            "the checkout can support. Preserve every passing PASS_TO_PASS test; if a "
            "PASS_TO_PASS failure is listed, first undo the regression before chasing new "
            "behavior. Prefer minimal changes to existing extension points over broad "
            "rewrites, and do not call helper methods unless you have verified they exist "
            "in the inspected class. If dependencies are missing locally, run a syntax "
            "check and leave the final patch for official re-evaluation.\n"
            "</official-swebench-feedback>"
        )

    def _retry_constraints(self, previous_patch: str = "") -> str:
        """Build the <regression-guard> or <retry-constraints> block.

        Replaces the old _format_retry_constraints() function.
        """
        if self.pass_to_pass:
            return _build_regression_guard(self.pass_to_pass, previous_patch)
        return (
            "<retry-constraints>\n"
            "No PASS_TO_PASS regression is recorded yet. Keep it that way: preserve the "
            f"{len(self.passing_tests)} official passing tests, make the smallest targeted change, "
            "avoid broad rewrites of already-working reader/writer paths, and when making "
            "matching case-insensitive also normalize downstream literal token consumers. "
            "Keep public APIs backward compatible, match asserted warning/stdout/error text "
            "exactly, including warning `hint` fields. For Django TextChoices/"
            "IntegerChoices bugs, preserve `str(member) == str(member.value)` and avoid "
            "broad enum coercion in model field/descriptor paths. For Django script-prefix URL bugs check `set_script_prefix()` / "
            "`get_script_prefix()` rather than assuming the source is a setting constant.\n"
            "</retry-constraints>"
        )


# ── PatchRiskItem / PatchRiskReport ──────────────────────────────────────────

@dataclass
class PatchRiskItem:
    """A single risk finding produced by PatchGuardrail.analyze()."""
    category: str   # e.g. "deleted_methods", "broad_except", "django_import_cycle"
    detail: str     # human-readable specifics (symbols, file names)
    severity: str   # "blocking" | "warning"


@dataclass
class PatchRiskReport:
    """Aggregated output of PatchGuardrail.analyze().

    Consumed by RetryOrchestrator to decide whether to apply the previous
    patch and to build anti-example blocks for the agent prompt.
    """
    items: list[PatchRiskItem] = field(default_factory=list)

    @property
    def has_blocking(self) -> bool:
        return any(i.severity == "blocking" for i in self.items)

    def risk_labels(self) -> list[str]:
        """Return labels in the legacy result['risk_reasons'] format.

        Called by orchestrator to populate the per-instance result dict,
        keeping downstream JSON report format unchanged.
        """
        labels = []
        for item in self.items:
            if item.detail:
                labels.append(f"patch_quality:{item.category}:{item.detail}")
            else:
                labels.append(f"patch_quality:{item.category}")
        return labels

    def to_prompt_block(self) -> str:
        """Serialise risk findings as a bullet list for the agent prompt.

        Replaces the old _format_previous_patch_risk_summary() function.
        Used when the previous patch is NOT applied (anti-example context).
        """
        if not self.items:
            bullets = ["- no structural risk detected by local parser"]
        else:
            bullets = []
            for item in self.items:
                line = f"- {item.category}"
                if item.detail:
                    line += f": {item.detail}"
                bullets.append(line)
        bullets.append("- required retry shape: subtract risky hunks, then add the smallest target fix")
        return "\n".join(bullets)


# ── RetryPlan ─────────────────────────────────────────────────────────────────

@dataclass
class RetryPlan:
    """RetryOrchestrator's decision for a single retry instance.

    Built by RetryOrchestrator.build_retry_plan(); consumed by
    RetryOrchestrator.run_instance() and build_initial_messages().
    """
    instance_id: str
    apply_previous_patch: bool      # whether to git-apply the previous patch first
    previous_patch: str
    failure_feedback: FailureFeedback | None
    risk_report: PatchRiskReport | None
    start_from_clean: bool          # True when previous patch is intentionally withheld
    empty_patch_retries: int


# ── Internal helpers ──────────────────────────────────────────────────────────

def _fmt_bullets(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _build_regression_guard(regression_tests: list[str], previous_patch: str) -> str:
    """Build the <regression-guard> block when PASS_TO_PASS regressions exist.

    Extracted from the old _format_retry_constraints() function.
    The previous_patch is analysed lazily here to avoid a circular import with
    guardrail — we only need the symbol names for the culprit hint lines,
    so we do a minimal inline parse rather than importing PatchGuardrail.
    """
    lines = [
        "<regression-guard>",
        "The previous patch regressed official PASS_TO_PASS tests. Treat this as a "
        "hard constraint, not a secondary concern.",
        "Priority order:",
        "1. First restore every listed PASS_TO_PASS regression.",
        "2. Then make the smallest change that still fixes the FAIL_TO_PASS behavior.",
        "3. Prefer reverting the specific broad hunk that caused the regression over "
        "adding more compensating code.",
        "4. Run or reason about both the regression test and the target failing test "
        "before finalizing.",
        "Hard rules:",
        "- Do not replace existing reader/writer paths wholesale.",
        "- Do not delete an existing override method that passing tests depended on "
        "unless you replace it with equivalent behavior.",
        "- Do not delete existing classes or move methods across class boundaries.",
        "- Do not add new read/write/process_lines methods under a regression guard; "
        "modify the existing narrow method instead.",
        "- Prefer tiny index/default/parameter-forwarding changes over new writer methods.",
        "- If behavior depends on a variable number of header rows, derive indexes from "
        "`len(header_rows)` instead of hard-coding `lines[0]` or `lines[1]`.",
        "- Do not add calls to helper methods unless you verified the method exists "
        "on the inspected object.",
        "- Do not use broad try/except as a fallback for unverified logic.",
        "- If you make classification or regex matching case-insensitive, also "
        "normalize every downstream token consumer that still compares literal "
        "sentinels such as `NO`.",
        "- Preserve public API signatures that tests or downstream users may call "
        "directly. If the failing tests call a method with more shapes than the "
        "current implementation supports, extend the signature conservatively "
        "instead of only fixing the internal call site.",
        "- Treat warning text, stdout/stderr text, and error messages in the "
        "official traceback as exact compatibility contracts. If a test asserts "
        "a message substring, the patch must emit that text, not merely reach the "
        "same final state.",
        "- For Django system checks, warning `id`, `msg`, and `hint` are all "
        "part of the asserted API. If the traceback shows `hint=...`, preserve "
        "that exact hint text.",
        "- For Django TextChoices/IntegerChoices bugs, preserve enum string "
        "semantics: `str(member)` should remain `str(member.value)`. Avoid broad "
        "coercion in `Field.get_prep_value()` or model descriptors unless the "
        "official traceback proves that exact path is required.",
        "- For Django URL prefix/static/media failures, inspect `django.urls."
        "set_script_prefix()` / `get_script_prefix()` and avoid top-level imports "
        "from `django.urls` inside core modules that can participate in import "
        "cycles. Prefer delayed imports inside the narrow function that needs it.",
        "- Do not sacrifice any PASS_TO_PASS behavior to satisfy one FAIL_TO_PASS test.",
    ]

    if previous_patch.strip():
        # Minimal inline parse to produce culprit hints without importing guardrail.
        # Full analysis is done by PatchGuardrail.analyze(); this is display-only.
        from nz_coder.swebench.guardrail import PatchGuardrail
        _g = PatchGuardrail()
        deleted = _g._parse_deleted_methods_raw(previous_patch)
        if deleted:
            lines.append(
                "Likely culprits - methods deleted by the previous patch "
                "(restoring or preserving these is the first priority):"
            )
            for filepath, methods in deleted.items():
                lines.append(f"  {filepath}: {', '.join(methods)}")
        risky_added = _g._parse_risky_added_methods_raw(previous_patch)
        if risky_added:
            lines.append(
                "Likely culprits - new reader/writer methods added by the previous "
                "patch under regression guard (remove these unless they are proven "
                "semantically required):"
            )
            for filepath, methods in risky_added.items():
                lines.append(f"  {filepath}: {', '.join(methods)}")
        if _g._raw_has_broad_except(previous_patch):
            lines.append(
                "Likely culprit - the previous patch added a broad except fallback. "
                "Remove it and fix the exact control flow instead."
            )
        if _g._raw_has_case_insensitive_no_norm(previous_patch):
            lines.append(
                "Likely culprit - the previous patch made matching case-insensitive "
                "but did not normalize downstream token consumers. Keep the matching "
                "change only if every literal sentinel comparison is normalized too."
            )

    lines.append("</regression-guard>")
    return "\n".join(lines)
