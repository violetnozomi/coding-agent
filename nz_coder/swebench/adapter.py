"""SWE-bench benchmark adapter.

SWEBenchAdapter is the only module that knows about:
  - the official SWE-bench Docker harness (swebench.harness.run_evaluation)
  - report.json / test_output.txt file layout
  - Docker image pre-pull mechanics

It converts official harness output into FailureFeedback and exposes a
small set of public methods used by the CLI and RetryOrchestrator.
"""
from __future__ import annotations

import importlib.util
import json
import multiprocessing
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

from nz_coder.swebench.models import FailureFeedback
from nz_coder.swebench.profiles import DEFAULT_PROFILE, get_profile


DATASET_NAME = get_profile(DEFAULT_PROFILE).dataset


class SWEBenchAdapter:
    """Thin wrapper around the official SWE-bench harness and its log files.

    Public interface
    ----------------
    check_environment()  → int          readiness check, returns exit code
    run_harness(...)     → int          invoke official Docker harness
    load_feedback(...)   → FailureFeedback
    load_predictions(p)  → dict[str, str]
    format_instance_prompt(instance) → str
    """

    def __init__(self, profile: str = DEFAULT_PROFILE):
        self.profile = get_profile(profile)

    # ── Environment / readiness ───────────────────────────────────────────────

    def check_environment(self) -> int:
        """Print readiness checks for running SWE-bench locally."""
        rows = [
            self._check_python(),
            self._check_module("swebench"),
            self._check_module("datasets"),
            self._check_executable("git"),
            self._check_docker(),
        ]
        print("# SWE-bench readiness\n")
        for ok, name, detail in rows:
            status = "OK" if ok else "MISSING"
            print(f"- [{status}] {name}: {detail}")

        if all(ok for ok, _, _ in rows):
            print("\nReady: local Docker-based SWE-bench evaluation can be attempted.")
            return 0

        print("\nNot ready: install the missing dependencies and make Docker daemon accessible before evaluation.")
        print("Minimum local requirements: `pip install swebench datasets` and a usable Docker daemon.")
        return 1

    # ── Official harness invocation ───────────────────────────────────────────

    def run_harness(self, predictions_path: Path, args) -> int:
        """Run the official SWE-bench harness for an existing predictions file."""
        if not predictions_path.exists():
            print(f"Error: predictions file not found: {predictions_path}")
            return 2

        if not self._check_module("swebench")[0]:
            print("Error: swebench is not installed. Install it with `pip install swebench`.")
            return 2
        docker_ok, _, docker_detail = self._check_docker()
        if not docker_ok:
            print(f"Error: Docker daemon is not usable: {docker_detail}")
            return 2

        instance_ids = list(args.instance_ids or [])
        if args.prepull_timeout:
            instance_ids = self._prepull_instance_images(
                instance_ids,
                timeout=args.prepull_timeout,
                skip_failures=args.skip_prepull_failures,
                namespace=args.image_namespace,
                arch=args.image_arch or platform.machine(),
                tag=args.instance_image_tag,
            )
            if args.instance_ids and not instance_ids:
                print("No instances left after image pre-pull filtering.")
                return 3

        cmd = [
            sys.executable,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            get_profile(getattr(args, "profile", DEFAULT_PROFILE)).dataset,
            "--predictions_path",
            str(predictions_path),
            "--max_workers",
            str(args.max_workers),
            "--run_id",
            args.run_id,
            "--timeout",
            str(args.timeout),
        ]
        if instance_ids:
            cmd.extend(["--instance_ids", *instance_ids])
        if args.clean:
            cmd.append("--clean")

        print("Running official SWE-bench harness:")
        print(" ".join(cmd))
        result = subprocess.run(cmd)
        return result.returncode

    # ── Feedback loading ──────────────────────────────────────────────────────

    def load_feedback(
        self,
        instance_id: str,
        eval_log_dir: Path,
        *,
        max_output_chars: int = 8000,
    ) -> FailureFeedback:
        """Read official harness logs and return a structured FailureFeedback.

        Replaces _read_official_feedback() + _official_feedback_summary().
        """
        instance_dir = self._resolve_instance_dir(instance_id, eval_log_dir)
        report = self._load_report(instance_id, instance_dir)
        tests_status = report.get("tests_status", {}) if report else {}

        fail_to_pass: list[str] = []
        pass_to_pass: list[str] = []
        passing_tests: list[str] = []

        for group_name, group in tests_status.items():
            for test_name in group.get("failure", []):
                item = f"{group_name}: {test_name}"
                fail_to_pass.append(item)
                if group_name.startswith("PASS_TO_"):
                    pass_to_pass.append(item)
            for test_name in group.get("success", []):
                passing_tests.append(f"{group_name}: {test_name}")

        output_path = instance_dir / "test_output.txt"
        raw_output = output_path.read_text(encoding="utf-8", errors="replace") if output_path.exists() else ""
        cleaned = _strip_ansi(raw_output)
        excerpt = _extract_failure_excerpt(cleaned, fail_to_pass, max_output_chars=max_output_chars)

        return FailureFeedback(
            instance_id=instance_id,
            resolved=report.get("resolved", "unknown") if report else "unknown",
            patch_applied=report.get("patch_successfully_applied", "unknown") if report else "unknown",
            fail_to_pass=fail_to_pass,
            pass_to_pass=pass_to_pass,
            passing_tests=passing_tests,
            output_excerpt=excerpt,
        )

    # ── Predictions file ──────────────────────────────────────────────────────

    def load_predictions(self, path: Path) -> dict[str, str]:
        """Load {instance_id: model_patch} from a JSONL predictions file."""
        if not path.exists():
            raise FileNotFoundError(f"predictions file not found: {path}")
        predictions: dict[str, str] = {}
        with path.open("r", encoding="utf-8") as fh:
            for line_number, line in enumerate(fh, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
                instance_id = row.get("instance_id")
                if instance_id:
                    predictions[instance_id] = row.get("model_patch") or ""
        return predictions

    # ── Instance prompt ───────────────────────────────────────────────────────

    def format_instance_prompt(self, instance: dict) -> str:
        """Build the initial user message for a SWE-bench instance."""
        parts = [
            f"Solve SWE-bench {self.profile.name.title()} instance `{instance['instance_id']}`.",
            f"Repository: {instance.get('repo')}",
            f"Base commit: {instance.get('base_commit')}",
            "",
            "Problem statement:",
            instance.get("problem_statement", "").strip(),
        ]
        parts.extend(["", "When finished, leave the repository with only the intended source-code changes."])
        return "\n".join(parts)

    def check_agent_dependencies(self) -> bool:
        """Return False (and print) if NZ-Coder runtime deps are missing."""
        missing = []
        for name in ("openai", "dotenv", "rich", "yaml"):
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        if missing:
            print("Error: missing NZ-Coder runtime dependencies: " + ", ".join(missing))
            print("Install project requirements before generating predictions.")
            return False
        return True

    # ── Private: environment checks ───────────────────────────────────────────

    @staticmethod
    def _check_python() -> tuple[bool, str, str]:
        version = ".".join(str(part) for part in sys.version_info[:3])
        ok = sys.version_info >= (3, 9)
        return ok, "python", version

    @staticmethod
    def _check_module(name: str) -> tuple[bool, str, str]:
        spec = importlib.util.find_spec(name)
        if spec is None:
            return False, name, "not installed"
        origin = spec.origin or "installed"
        return True, name, origin

    @staticmethod
    def _check_executable(name: str) -> tuple[bool, str, str]:
        path = shutil.which(name)
        if path is None:
            return False, name, "not found on PATH"
        return True, name, path

    @staticmethod
    def _check_docker() -> tuple[bool, str, str]:
        docker_cli = shutil.which("docker")
        docker_sock = Path("/var/run/docker.sock")
        if docker_cli is None and not docker_sock.exists():
            return False, "docker", "Docker CLI/socket not found"

        if docker_cli is not None:
            try:
                result = subprocess.run(
                    [docker_cli, "version", "--format", "{{.Server.Version}}"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return False, "docker", f"daemon check failed: {exc}"
            detail = (result.stdout or result.stderr).strip()
            if result.returncode == 0:
                return True, "docker", f"{docker_cli} server={detail or 'unknown'}"
            if not detail:
                detail_result = subprocess.run(
                    [docker_cli, "version"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=10,
                )
                detail = (detail_result.stdout + detail_result.stderr).strip()
            return False, "docker", f"{docker_cli} present but daemon unavailable: {detail or 'unknown error'}"

        try:
            import docker
            client = docker.from_env()
            client.ping()
        except Exception as exc:
            return False, "docker", f"socket present but daemon unavailable: {type(exc).__name__}: {exc}"
        return True, "docker", f"daemon reachable through {docker_sock}"

    # ── Private: log file resolution ──────────────────────────────────────────

    def _resolve_instance_dir(self, instance_id: str, eval_log_dir: Path) -> Path:
        candidates = [
            eval_log_dir,
            eval_log_dir / instance_id,
            eval_log_dir / _safe_name(instance_id),
        ]
        for candidate in candidates:
            if (candidate / "report.json").exists() or (candidate / "test_output.txt").exists():
                return candidate
        matches = list(eval_log_dir.glob(f"**/{instance_id}/report.json"))
        if matches:
            return matches[0].parent
        matches = list(eval_log_dir.glob(f"**/{_safe_name(instance_id)}/report.json"))
        if matches:
            return matches[0].parent
        raise FileNotFoundError(
            f"could not find official eval logs for {instance_id} under {eval_log_dir}"
        )

    @staticmethod
    def _load_report(instance_id: str, instance_dir: Path) -> dict:
        report_path = instance_dir / "report.json"
        if not report_path.exists():
            return {}
        report = json.loads(report_path.read_text(encoding="utf-8"))
        return report.get(instance_id, report)

    # ── Private: Docker image pre-pull ────────────────────────────────────────

    def _prepull_instance_images(
        self,
        instance_ids: list[str],
        *,
        timeout: int,
        skip_failures: bool,
        namespace: str,
        arch: str,
        tag: str,
    ) -> list[str]:
        if not instance_ids:
            print("Image pre-pull skipped: pass --instance-ids to use --prepull-timeout.")
            return instance_ids

        ready: list[str] = []
        for instance_id in instance_ids:
            image = _instance_image_name(instance_id, namespace=namespace, arch=arch, tag=tag)
            print(f"Pre-pulling official image for {instance_id}: {image}")
            ok, detail = _docker_pull_with_timeout(image, timeout)
            if ok:
                print(f"Image ready: {image}")
                ready.append(instance_id)
                continue
            print(f"Image pre-pull failed for {instance_id}: {detail}")
            if not skip_failures:
                raise SystemExit(3)
            print(f"Skipping infra-blocked instance: {instance_id}")
        return ready


# ── Module-level utilities ────────────────────────────────────────────────────

def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in value)


def _strip_ansi(text: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text)


def _extract_failure_excerpt(text: str, failing_tests: list[str], *, max_output_chars: int) -> str:
    if not text:
        return ""
    markers = ["FAILURES", "FAILED "]
    for test_name in failing_tests:
        markers.append(test_name.split(": ", 1)[-1])
        markers.append(test_name.rsplit("::", 1)[-1])
    for marker in markers:
        index = text.find(marker)
        if index >= 0:
            start = max(0, index - max_output_chars // 5)
            end = min(len(text), index + max_output_chars)
            return text[start:end].strip()
    return text[-max_output_chars:].strip()


def _instance_image_name(
    instance_id: str,
    *,
    namespace: str = "swebench",
    arch: str = "x86_64",
    tag: str = "latest",
) -> str:
    key = f"sweb.eval.{arch}.{instance_id.lower()}:{tag}"
    if namespace:
        return f"{namespace}/{key}".replace("__", "_1776_")
    return key


def _docker_pull_with_timeout(image: str, timeout: int) -> tuple[bool, str]:
    queue: multiprocessing.Queue = multiprocessing.Queue()
    process = multiprocessing.Process(target=_docker_pull_worker, args=(image, queue), daemon=True)
    process.start()
    process.join(timeout)
    if process.is_alive():
        process.terminate()
        process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        return False, f"timeout after {timeout}s"

    if not queue.empty():
        result = queue.get()
        return bool(result.get("ok")), str(result.get("detail", ""))
    if process.exitcode == 0:
        return True, "completed"
    return False, f"docker pull worker exited with code {process.exitcode}"


def _docker_pull_worker(image: str, queue: multiprocessing.Queue) -> None:
    try:
        import docker
        client = docker.from_env()
        client.images.pull(image)
        queue.put({"ok": True, "detail": "pulled"})
    except Exception as exc:
        queue.put({"ok": False, "detail": f"{type(exc).__name__}: {exc}"})
