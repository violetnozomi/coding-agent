# SWE-bench Trace Budget Design

## Goal

Retain useful per-instance diagnostic evidence while keeping SWE-bench disk use
bounded to 20 GiB and preserving strict pass@1 reproducibility.

## Lifecycle

After an instance finishes, NZ-Coder must first persist its prediction, attempt
journal result, and public trajectory. It then archives a diagnostic bundle
containing the raw JSONL trace, public inference input, session JSON, and a
metadata file with instance status and patch length. Only after the archive is
durable may it delete the source checkout.

The archive root is run-scoped. Its exact byte size is recomputed after every
bundle. At 18 GiB the runner prints a warning. At 20 GiB it stops before
claiming the next instance, writes a machine-readable budget report, and exits
with a distinct incomplete status. Predictions and completed attempts remain
resumable.

## Analysis and cleanup

The runner never automatically deletes diagnostic bundles. When the 20 GiB
gate stops a run, Codex reviews traces for recurring API errors, empty patches,
tool-policy failures, max-turn exhaustion, inefficient search, and verification
failures. Findings are recorded in `docs/swebench-progress.md`. Only bundles
covered by that analysis may be removed, oldest first, until usage is at or
below 15 GiB; the run can then resume.

Public trajectories, predictions, manifests, attempt journals, analysis
reports, and official Docker harness logs are never part of trace cleanup.
Source checkouts continue to be deleted per completed instance.

## Failure behavior

Archive failure stops the batch after durable prediction persistence and before
checkout deletion. A path may be archived or deleted only when it is a direct
child of the configured run root. Interrupted instances remain claimed under
strict pass@1 and retain their checkout until explicitly exported and cleaned.

## Verification

Tests cover bundle contents, atomic archive publication, direct-child path
validation, warning and hard-limit decisions, pre-claim hard-limit behavior,
and preservation of the checkout when archival fails.
