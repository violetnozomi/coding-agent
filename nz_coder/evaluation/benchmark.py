"""Benchmark: automated evaluation of NZ-Coder on coding tasks.

Usage:
    python -m nz_coder.benchmark                # run all tasks
    python -m nz_coder.benchmark --task fizzbuzz # run one task
    python -m nz_coder.benchmark --report        # show last report

Each task defines:
  - task_id: unique identifier
  - description: what the agent is asked to do
  - setup(): prepare workspace (create initial files if needed)
  - verify(): check if the agent's output is correct (returns pass/fail + reason)
  - cleanup(): remove generated files

The benchmark runs the agent in non-streaming auto-permission mode,
captures all tool calls, and produces a structured report.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nz_coder import config
from nz_coder.prompt import build
from nz_coder.runtime.composition import build_product_environment
from nz_coder.runtime.workdir import scoped_workdir
from nz_coder.trace import TraceRecorder

BENCH_DIR = config.WORKDIR / ".nz-coder" / "benchmark"
REPORT_PATH = BENCH_DIR / "report.json"
REPORT_MD_PATH = BENCH_DIR / "report.md"


# ============================================================
# Task definitions
# ============================================================

class BenchTask:
    task_id: str = ""
    description: str = ""
    difficulty: str = "easy"  # easy / medium / hard
    task_type: str = "general"
    max_turns: int = 15

    def setup(self):
        """Prepare workspace files for this task."""
        pass

    def verify(self) -> dict:
        """Check result. Returns {"passed": bool, "reason": str}."""
        return {"passed": False, "reason": "Not implemented"}

    def cleanup(self):
        """Remove generated files."""
        pass


class FizzBuzz(BenchTask):
    task_id = "fizzbuzz"
    description = "Create a file fizzbuzz.py that prints FizzBuzz from 1 to 100. For multiples of 3 print 'Fizz', multiples of 5 print 'Buzz', multiples of both print 'FizzBuzz', otherwise print the number."
    difficulty = "easy"
    task_type = "file_create"

    def cleanup(self):
        (BENCH_DIR / "fizzbuzz.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        fp = BENCH_DIR / "fizzbuzz.py"
        if not fp.exists():
            return {"passed": False, "reason": "fizzbuzz.py not created"}
        try:
            result = subprocess.run(
                [sys.executable, str(fp)],
                capture_output=True, text=True, timeout=10, cwd=str(BENCH_DIR)
            )
            output = result.stdout.strip()
            lines = output.splitlines()
            if len(lines) < 100:
                return {"passed": False, "reason": f"Expected 100 lines, got {len(lines)}"}
            checks = [
                (lines[0], "1"),
                (lines[2], "Fizz"),
                (lines[4], "Buzz"),
                (lines[14], "FizzBuzz"),
                (lines[99], "Buzz"),
            ]
            for actual, expected in checks:
                if actual.strip() != expected:
                    return {"passed": False, "reason": f"Expected '{expected}', got '{actual}'"}
            return {"passed": True, "reason": "All checks passed"}
        except Exception as e:
            return {"passed": False, "reason": f"Execution error: {e}"}


class BugFix(BenchTask):
    task_id = "bugfix_sum"
    description = "The file bugfix_sum.py has a bug. Fix it so that sum_list([1,2,3,4,5]) returns 15."
    difficulty = "easy"
    task_type = "bugfix"

    def setup(self):
        # Intentional bug: starts with total=1 instead of 0
        code = '''def sum_list(numbers):
    total = 1
    for n in numbers:
        total += n
    return total

if __name__ == "__main__":
    result = sum_list([1, 2, 3, 4, 5])
    print(f"Sum: {result}")
'''
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "bugfix_sum.py").write_text(code)

    def cleanup(self):
        (BENCH_DIR / "bugfix_sum.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        fp = BENCH_DIR / "bugfix_sum.py"
        if not fp.exists():
            return {"passed": False, "reason": "File not found"}
        try:
            result = subprocess.run(
                [sys.executable, str(fp)],
                capture_output=True, text=True, timeout=10, cwd=str(BENCH_DIR)
            )
            if "15" in result.stdout:
                return {"passed": True, "reason": "Output contains 15"}
            return {"passed": False, "reason": f"Expected 15, got: {result.stdout.strip()}"}
        except Exception as e:
            return {"passed": False, "reason": f"Error: {e}"}


class AddFunction(BenchTask):
    task_id = "add_function"
    description = "Read the file math_utils.py and add a function called `factorial(n)` that computes n! recursively. Do not modify existing functions."
    difficulty = "medium"
    task_type = "feature_add"

    def setup(self):
        code = '''def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
'''
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "math_utils.py").write_text(code)

    def cleanup(self):
        (BENCH_DIR / "math_utils.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        fp = BENCH_DIR / "math_utils.py"
        if not fp.exists():
            return {"passed": False, "reason": "File not found"}
        content = fp.read_text()
        if "def add" not in content or "def multiply" not in content:
            return {"passed": False, "reason": "Existing functions were removed"}
        if "def factorial" not in content:
            return {"passed": False, "reason": "factorial function not added"}
        try:
            test_code = f"""
import sys
sys.path.insert(0, r'{BENCH_DIR}')
from math_utils import factorial, add, multiply
assert add(2, 3) == 5, "add broken"
assert multiply(3, 4) == 12, "multiply broken"
assert factorial(0) == 1, f"factorial(0)={{factorial(0)}}"
assert factorial(1) == 1, f"factorial(1)={{factorial(1)}}"
assert factorial(5) == 120, f"factorial(5)={{factorial(5)}}"
print("OK")
"""
            result = subprocess.run(
                [sys.executable, "-c", test_code],
                capture_output=True, text=True, timeout=10
            )
            if "OK" in result.stdout:
                return {"passed": True, "reason": "All assertions passed"}
            err = (result.stderr + result.stdout).strip()
            return {"passed": False, "reason": f"Test failed: {err[:200]}"}
        except Exception as e:
            return {"passed": False, "reason": f"Error: {e}"}


class WriteTests(BenchTask):
    task_id = "write_tests"
    description = "Read the file string_utils.py and create a test file test_string_utils.py that tests all functions using pytest-style assertions. Each function should have at least 2 test cases."
    difficulty = "medium"
    task_type = "test_authoring"

    def setup(self):
        code = '''def reverse_string(s):
    return s[::-1]

def is_palindrome(s):
    cleaned = s.lower().replace(" ", "")
    return cleaned == cleaned[::-1]

def count_vowels(s):
    return sum(1 for c in s.lower() if c in "aeiou")
'''
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "string_utils.py").write_text(code)

    def cleanup(self):
        (BENCH_DIR / "string_utils.py").unlink(missing_ok=True)
        (BENCH_DIR / "test_string_utils.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        test_fp = BENCH_DIR / "test_string_utils.py"
        if not test_fp.exists():
            return {"passed": False, "reason": "test_string_utils.py not created"}
        content = test_fp.read_text()
        # Check that test functions exist
        func_count = content.count("def test_")
        if func_count < 3:
            return {"passed": False, "reason": f"Only {func_count} test functions (need >= 3)"}
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", str(test_fp), "-v", "--tb=short"],
                capture_output=True, text=True, timeout=30, cwd=str(BENCH_DIR)
            )
            if result.returncode == 0:
                return {"passed": True, "reason": f"All {func_count} tests passed"}
            # Try running without pytest
            result2 = subprocess.run(
                [sys.executable, str(test_fp)],
                capture_output=True, text=True, timeout=10, cwd=str(BENCH_DIR)
            )
            if result2.returncode == 0:
                return {"passed": True, "reason": "Tests passed (direct execution)"}
            return {"passed": False, "reason": f"Tests failed: {result.stdout[-300:]}"}
        except Exception as e:
            return {"passed": False, "reason": f"Error: {e}"}


class RefactorClass(BenchTask):
    task_id = "refactor_class"
    description = (
        "Refactor the file user_manager.py: extract the email validation logic into a module-level "
        "`validate_email(email)` function that returns True/False, and update `UserManager.create_user()` "
        "to call it. First call load_optional_tools with the python_ast pack, then prefer "
        "python_structural_edit for this task: insert `validate_email` before "
        "`UserManager` and replace `UserManager.create_user` by symbol. The behavior must remain "
        "identical. After editing, verify with python_symbol_check "
        "that `validate_email`, `UserManager`, and `UserManager.create_user` exist and that "
        "`UserManager.create_user` calls `validate_email`. You must also run this behavior check and fix "
        "any failure before finishing, using the exact Python interpreter shown here: "
        "{PYTHON} -c \"from user_manager import UserManager, validate_email; "
        "assert validate_email('a@b.c') is True; assert validate_email('') is False; "
        "assert validate_email('noat') is False; assert validate_email('no@dot') is False; "
        "um=UserManager(); assert um.create_user('Test','test@example.com')['email']=='test@example.com'; print('OK')\""
    )
    difficulty = "hard"
    task_type = "refactor"

    def setup(self):
        code = '''class UserManager:
    def __init__(self):
        self.users = {}

    def create_user(self, name, email):
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            raise ValueError(f"Invalid email: {email}")
        if email in self.users:
            raise ValueError(f"User already exists: {email}")
        self.users[email] = {"name": name, "email": email}
        return self.users[email]

    def get_user(self, email):
        return self.users.get(email)

    def delete_user(self, email):
        if email in self.users:
            del self.users[email]
            return True
        return False
'''
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "user_manager.py").write_text(code)

    def cleanup(self):
        (BENCH_DIR / "user_manager.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        fp = BENCH_DIR / "user_manager.py"
        if not fp.exists():
            return {"passed": False, "reason": "File not found"}
        content = fp.read_text()
        if "def validate_email" not in content:
            return {"passed": False, "reason": "validate_email function not found"}
        if "validate_email" not in content.split("def create_user")[1] if "def create_user" in content else "":
            return {"passed": False, "reason": "create_user doesn't use validate_email"}
        try:
            test_code = f"""
import sys
sys.path.insert(0, r'{BENCH_DIR}')
from user_manager import UserManager, validate_email
# validate_email tests
assert validate_email("a@b.c") == True
assert validate_email("") == False
assert validate_email("noat") == False
assert validate_email("no@dot") == False
# UserManager tests
um = UserManager()
u = um.create_user("Test", "test@example.com")
assert u["name"] == "Test"
assert um.get_user("test@example.com") is not None
try:
    um.create_user("Dup", "test@example.com")
    assert False, "Should have raised"
except ValueError:
    pass
try:
    um.create_user("Bad", "invalid")
    assert False, "Should have raised"
except ValueError:
    pass
assert um.delete_user("test@example.com") == True
assert um.delete_user("nobody@x.com") == False
print("OK")
"""
            result = subprocess.run(
                [sys.executable, "-c", test_code],
                capture_output=True, text=True, timeout=10
            )
            if "OK" in result.stdout:
                return {"passed": True, "reason": "All assertions passed"}
            err = (result.stderr + result.stdout).strip()
            return {"passed": False, "reason": f"Test failed: {err[:300]}"}
        except Exception as e:
            return {"passed": False, "reason": f"Error: {e}"}


class MultiFileCreate(BenchTask):
    task_id = "multi_file"
    description = "Create a Python package called 'calculator' with: __init__.py (exports add, subtract, multiply, divide), operations.py (implements the 4 functions, divide raises ZeroDivisionError for 0), and test_calculator.py."
    difficulty = "hard"
    task_type = "multi_file"

    def cleanup(self):
        pkg = BENCH_DIR / "calculator"
        if pkg.exists():
            shutil.rmtree(str(pkg))
        (BENCH_DIR / "test_calculator.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        pkg = BENCH_DIR / "calculator"
        if not pkg.exists():
            return {"passed": False, "reason": "calculator/ package not created"}
        if not (pkg / "__init__.py").exists():
            return {"passed": False, "reason": "__init__.py missing"}
        if not (pkg / "operations.py").exists():
            return {"passed": False, "reason": "operations.py missing"}
        try:
            test_code = f"""
import sys
sys.path.insert(0, r'{BENCH_DIR}')
from calculator import add, subtract, multiply, divide
assert add(2, 3) == 5
assert subtract(10, 4) == 6
assert multiply(3, 7) == 21
assert divide(10, 2) == 5.0 or divide(10, 2) == 5
try:
    divide(1, 0)
    assert False, "Should raise ZeroDivisionError"
except ZeroDivisionError:
    pass
print("OK")
"""
            result = subprocess.run(
                [sys.executable, "-c", test_code],
                capture_output=True, text=True, timeout=10
            )
            if "OK" in result.stdout:
                return {"passed": True, "reason": "All assertions passed"}
            err = (result.stderr + result.stdout).strip()
            return {"passed": False, "reason": f"Test failed: {err[:300]}"}
        except Exception as e:
            return {"passed": False, "reason": f"Error: {e}"}


class BoundaryBugFix(BenchTask):
    task_id = "boundary_bugfix"
    description = "The file age_check.py has a boundary bug. Fix it so that is_adult(age) returns True for age 18 and above, and False below 18."
    difficulty = "easy"
    task_type = "bugfix"

    def setup(self):
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "age_check.py").write_text('''def is_adult(age):
    return age > 18
''')

    def cleanup(self):
        (BENCH_DIR / "age_check.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        fp = BENCH_DIR / "age_check.py"
        if not fp.exists():
            return {"passed": False, "reason": "age_check.py not found"}
        test_code = f"""
import sys
sys.path.insert(0, r'{BENCH_DIR}')
from age_check import is_adult
assert is_adult(17) is False
assert is_adult(18) is True
assert is_adult(21) is True
print("OK")
"""
        return _verify_python(test_code)


class PytestRepair(BenchTask):
    task_id = "pytest_repair"
    description = "Run or inspect the tests for calc.py, then fix calc.py so all tests in test_calc.py pass. Do not weaken the tests."
    difficulty = "medium"
    task_type = "test_repair"

    def setup(self):
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "calc.py").write_text('''def add(a, b):
    return a + b

def subtract(a, b):
    return a + b
''')
        (BENCH_DIR / "test_calc.py").write_text('''from calc import add, subtract

def test_add():
    assert add(2, 3) == 5

def test_subtract():
    assert subtract(5, 2) == 3
    assert subtract(2, 5) == -3
''')

    def cleanup(self):
        (BENCH_DIR / "calc.py").unlink(missing_ok=True)
        (BENCH_DIR / "test_calc.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        tests = (BENCH_DIR / "test_calc.py").read_text() if (BENCH_DIR / "test_calc.py").exists() else ""
        if "subtract(5, 2) == 3" not in tests:
            return {"passed": False, "reason": "test_calc.py was weakened"}
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "test_calc.py", "-q"],
            cwd=str(BENCH_DIR), capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return {"passed": True, "reason": "pytest passed"}
        return {"passed": False, "reason": f"pytest failed: {(result.stdout + result.stderr)[-300:]}"}


class MultiFileBugFix(BenchTask):
    task_id = "multi_file_bugfix"
    description = "The small app split across app.py and helpers.py prints an incorrect total. Fix the implementation so running app.py prints 'Total: 12'."
    difficulty = "medium"
    task_type = "multi_file"

    def setup(self):
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "helpers.py").write_text('''def normalize(values):
    return [int(v) for v in values]

def total(values):
    result = 1
    for value in values:
        result += value
    return result
''')
        (BENCH_DIR / "app.py").write_text('''from helpers import normalize, total

values = normalize(["3", "4", "5"])
print(f"Total: {total(values)}")
''')

    def cleanup(self):
        (BENCH_DIR / "helpers.py").unlink(missing_ok=True)
        (BENCH_DIR / "app.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        result = subprocess.run(
            [sys.executable, "app.py"], cwd=str(BENCH_DIR),
            capture_output=True, text=True, timeout=10
        )
        if result.stdout.strip() == "Total: 12":
            return {"passed": True, "reason": "app output is correct"}
        return {"passed": False, "reason": f"expected Total: 12, got {result.stdout.strip()}"}


class CliArgparse(BenchTask):
    task_id = "cli_argparse"
    description = "Update greet.py so it accepts a --name argument and prints 'Hello, <name>!'. Keep the default output as 'Hello, world!'."
    difficulty = "medium"
    task_type = "cli"

    def setup(self):
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "greet.py").write_text('''def main():
    print("Hello, world!")

if __name__ == "__main__":
    main()
''')

    def cleanup(self):
        (BENCH_DIR / "greet.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        default = subprocess.run([sys.executable, "greet.py"], cwd=str(BENCH_DIR), capture_output=True, text=True, timeout=10)
        named = subprocess.run([sys.executable, "greet.py", "--name", "Nozomi"], cwd=str(BENCH_DIR), capture_output=True, text=True, timeout=10)
        if default.stdout.strip() == "Hello, world!" and named.stdout.strip() == "Hello, Nozomi!":
            return {"passed": True, "reason": "CLI behavior correct"}
        return {"passed": False, "reason": f"default={default.stdout.strip()} named={named.stdout.strip()}"}


class JsonConfigUpdate(BenchTask):
    task_id = "json_config_update"
    description = "Update settings.json: set retries to 5, enable telemetry, and add 'benchmark' to the features list without removing existing keys."
    difficulty = "easy"
    task_type = "structured_edit"

    def setup(self):
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "settings.json").write_text(json.dumps({
            "name": "demo",
            "retries": 2,
            "telemetry": False,
            "features": ["memory", "tools"],
        }, indent=2))

    def cleanup(self):
        (BENCH_DIR / "settings.json").unlink(missing_ok=True)

    def verify(self) -> dict:
        fp = BENCH_DIR / "settings.json"
        if not fp.exists():
            return {"passed": False, "reason": "settings.json missing"}
        try:
            data = json.loads(fp.read_text())
        except json.JSONDecodeError as e:
            return {"passed": False, "reason": f"invalid JSON: {e}"}
        ok = (
            data.get("name") == "demo" and
            data.get("retries") == 5 and
            data.get("telemetry") is True and
            set(data.get("features", [])) >= {"memory", "tools", "benchmark"}
        )
        return {"passed": ok, "reason": "settings updated" if ok else f"unexpected data: {data}"}


class PublicApiPreserve(BenchTask):
    task_id = "public_api_preserve"
    description = "Refactor temperature.py to remove duplication by adding a helper if useful. Preserve the public functions c_to_f and f_to_c exactly."
    difficulty = "hard"
    task_type = "refactor"

    def setup(self):
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "temperature.py").write_text('''def c_to_f(celsius):
    return celsius * 9 / 5 + 32

def f_to_c(fahrenheit):
    return (fahrenheit - 32) * 5 / 9
''')

    def cleanup(self):
        (BENCH_DIR / "temperature.py").unlink(missing_ok=True)

    def verify(self) -> dict:
        fp = BENCH_DIR / "temperature.py"
        if not fp.exists():
            return {"passed": False, "reason": "temperature.py missing"}
        content = fp.read_text()
        if "def c_to_f" not in content or "def f_to_c" not in content:
            return {"passed": False, "reason": "public API was not preserved"}
        test_code = f"""
import sys
sys.path.insert(0, r'{BENCH_DIR}')
from temperature import c_to_f, f_to_c
assert round(c_to_f(0), 4) == 32
assert round(c_to_f(100), 4) == 212
assert round(f_to_c(32), 4) == 0
assert round(f_to_c(212), 4) == 100
print("OK")
"""
        return _verify_python(test_code)


class DocumentationUpdate(BenchTask):
    task_id = "documentation_update"
    description = "Update README_TASK.md to document the three available commands: run, test, and benchmark. Keep the existing title."
    difficulty = "easy"
    task_type = "documentation"

    def setup(self):
        BENCH_DIR.mkdir(parents=True, exist_ok=True)
        (BENCH_DIR / "README_TASK.md").write_text("# Demo Tool\n\nTODO: document commands.\n")

    def cleanup(self):
        (BENCH_DIR / "README_TASK.md").unlink(missing_ok=True)

    def verify(self) -> dict:
        fp = BENCH_DIR / "README_TASK.md"
        if not fp.exists():
            return {"passed": False, "reason": "README_TASK.md missing"}
        text = fp.read_text().lower()
        ok = text.startswith("# demo tool") and all(word in text for word in ("run", "test", "benchmark"))
        return {"passed": ok, "reason": "docs updated" if ok else "missing title or command docs"}


def _verify_python(test_code: str) -> dict:
    try:
        result = subprocess.run([sys.executable, "-c", test_code], capture_output=True, text=True, timeout=10)
        if "OK" in result.stdout:
            return {"passed": True, "reason": "All assertions passed"}
        return {"passed": False, "reason": f"Test failed: {(result.stderr + result.stdout).strip()[:300]}"}
    except Exception as e:
        return {"passed": False, "reason": f"Error: {e}"}


# ============================================================
# All tasks
# ============================================================

ALL_TASKS = [
    FizzBuzz(),
    BugFix(),
    AddFunction(),
    WriteTests(),
    RefactorClass(),
    MultiFileCreate(),
    BoundaryBugFix(),
    PytestRepair(),
    MultiFileBugFix(),
    CliArgparse(),
    JsonConfigUpdate(),
    PublicApiPreserve(),
    DocumentationUpdate(),
]

TASK_MAP = {t.task_id: t for t in ALL_TASKS}


# ============================================================
# Runner
# ============================================================

async def run_task(task: BenchTask, verbose: bool = True) -> dict:
    """Run a single benchmark task. Returns result dict."""
    BENCH_DIR.mkdir(parents=True, exist_ok=True)

    task.cleanup()
    task.setup()

    with scoped_workdir(BENCH_DIR):
        system_prompt = build() + (
            f"\n\nYou are working in: {BENCH_DIR}\n"
            "Complete the task efficiently. Do not ask questions."
        )
        tracer = TraceRecorder(trace_dir=BENCH_DIR / "runs", enabled=True)
        agent = build_product_environment(
            system_prompt, permission_mode="auto", tracer=tracer,
        )
    task_description = task.description.replace("{PYTHON}", sys.executable)
    messages = [{"role": "user", "content": task_description}]

    tool_log = []
    def log_tool(name, output):
        tool_log.append({
            "tool": name,
            "output_len": len(output),
            "status": "error" if output.startswith("Error:") or output.startswith("Denied") else "ok",
        })
        if verbose:
            print(f"  [{task.task_id}] {name}: {_safe_console(output[:80])}")

    start = time.time()
    try:
        run_status = await agent.run(messages, on_tool=log_tool, stream=False)
    except Exception as e:
        reason = f"Agent error: {e}"
        return {
            "task_id": task.task_id,
            "difficulty": task.difficulty,
            "task_type": task.task_type,
            "passed": False,
            "reason": reason,
            "failure_category": classify_failure(reason),
            "duration": time.time() - start,
            "turns": len([m for m in messages if m.get("role") == "assistant"]),
            "tool_calls": len(tool_log),
            "tool_errors": sum(1 for t in tool_log if t["status"] == "error"),
            "trace": str(tracer.path),
        }
    finally:
        agent.close()

    duration = time.time() - start
    # completed_unverified 是正常完成状态（验证门提示次数达上限），应继续走 task.verify()
    # 只有 aborted / max_turns / error 才视为真正失败
    _HARD_FAIL_STATUSES = {"aborted", "max_turns", "error"}
    if run_status and run_status.get("status") in _HARD_FAIL_STATUSES:
        reason = f"Agent {run_status.get('status')}: {run_status.get('last_error') or run_status.get('errors')}"
        return {
            "task_id": task.task_id,
            "difficulty": task.difficulty,
            "task_type": task.task_type,
            "passed": False,
            "reason": reason,
            "failure_category": classify_failure(reason),
            "duration": round(duration, 1),
            "turns": len([m for m in messages if m.get("role") == "assistant"]),
            "tool_calls": len(tool_log),
            "tool_errors": sum(1 for t in tool_log if t["status"] == "error"),
            "trace": str(tracer.path),
        }

    verify_result = task.verify()

    return {
        "task_id": task.task_id,
        "difficulty": task.difficulty,
        "task_type": task.task_type,
        "passed": verify_result["passed"],
        "reason": verify_result["reason"],
        "failure_category": None if verify_result["passed"] else classify_failure(verify_result["reason"]),
        "duration": round(duration, 1),
        "turns": len([m for m in messages if m.get("role") == "assistant"]),
        "tool_calls": len(tool_log),
        "tool_errors": sum(1 for t in tool_log if t["status"] == "error"),
        "trace": str(tracer.path),
    }


async def run_all(verbose: bool = True) -> dict:
    """Run all benchmark tasks and produce a report."""
    results = []
    for task in ALL_TASKS:
        if verbose:
            print(f"\n{'='*50}")
            print(f"Task: {task.task_id} ({task.difficulty})")
            print(f"{'='*50}")
        result = await run_task(task, verbose)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        if verbose:
            print(f"  -> {status}: {result['reason']} ({result['duration']}s)")

    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    summary = summarize_results(results)

    report = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": config.MODEL_ID,
        "total": total,
        "passed": passed,
        "pass_rate": f"{passed/total*100:.0f}%" if total else "N/A",
        "summary": summary,
        "results": results,
    }

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    REPORT_MD_PATH.write_text(render_markdown_report(report), encoding="utf-8")

    if verbose:
        print(f"\n{'='*50}")
        print(f"RESULT: {passed}/{total} passed ({report['pass_rate']})")
        print(f"Report saved: {REPORT_PATH}")
        print(f"Markdown saved: {REPORT_MD_PATH}")
        print(f"{'='*50}")

    return report


def show_report():
    """Show the last benchmark report."""
    if not REPORT_PATH.exists():
        print("No benchmark report found. Run benchmark first.")
        return
    report = json.loads(REPORT_PATH.read_text())
    print(f"\nBenchmark Report ({report['timestamp']})")
    print(f"Model: {report['model']}")
    print(f"Pass rate: {report['pass_rate']} ({report['passed']}/{report['total']})\n")
    summary = report.get("summary", {})
    if summary:
        print(f"Avg turns: {summary.get('avg_turns')} | Avg tools: {summary.get('avg_tool_calls')} | Avg time: {summary.get('avg_duration')}s\n")
    for r in report["results"]:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"  [{status}] {r['task_id']:20s} ({r['difficulty']:6s}/{r.get('task_type', '-'):14s}) "
              f"turns={r['turns']} tools={r['tool_calls']} time={r['duration']}s")
        if not r["passed"]:
            print(f"         reason: {r['reason']}")


def _safe_console(text: str) -> str:
    encoding = sys.stdout.encoding or "utf-8"
    return str(text).encode(encoding, errors="replace").decode(encoding, errors="replace")


def summarize_results(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if r["passed"])
    by_difficulty = _group_stats(results, "difficulty")
    by_type = _group_stats(results, "task_type")
    failures = defaultdict(int)
    for result in results:
        if not result["passed"]:
            failures[result.get("failure_category") or "unknown"] += 1
    return {
        "pass_rate": round(passed / total, 3) if total else 0,
        "avg_turns": round(sum(r["turns"] for r in results) / total, 2) if total else 0,
        "avg_tool_calls": round(sum(r["tool_calls"] for r in results) / total, 2) if total else 0,
        "avg_duration": round(sum(float(r["duration"]) for r in results) / total, 2) if total else 0,
        "by_difficulty": by_difficulty,
        "by_type": by_type,
        "failure_categories": dict(sorted(failures.items())),
    }


def _group_stats(results: list[dict], key: str) -> dict:
    grouped = defaultdict(list)
    for result in results:
        grouped[result.get(key, "-")].append(result)
    stats = {}
    for name, rows in sorted(grouped.items()):
        passed = sum(1 for r in rows if r["passed"])
        stats[name] = {
            "passed": passed,
            "total": len(rows),
            "pass_rate": round(passed / len(rows), 3) if rows else 0,
        }
    return stats


def classify_failure(reason: str) -> str:
    text = (reason or "").lower()
    if "not created" in text or "missing" in text or "not found" in text:
        return "missing_artifact"
    if "pytest failed" in text or "test failed" in text or "assertion" in text:
        return "incorrect_behavior"
    if "agent error" in text or "api" in text:
        return "agent_error"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "weakened" in text or "removed" in text or "public api" in text:
        return "regression"
    return "verification_failed"


def render_markdown_report(report: dict) -> str:
    summary = report.get("summary", {})
    lines = [
        "# NZ-Coder Benchmark Report",
        "",
        f"- Timestamp: {report['timestamp']}",
        f"- Model: `{report['model']}`",
        f"- Pass rate: **{report['pass_rate']}** ({report['passed']}/{report['total']})",
        f"- Avg turns/tools/time: {summary.get('avg_turns', 0)} / {summary.get('avg_tool_calls', 0)} / {summary.get('avg_duration', 0)}s",
        "",
        "## Results",
        "",
        "| Task | Type | Difficulty | Result | Turns | Tools | Time | Reason |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in report["results"]:
        status = "PASS" if r["passed"] else "FAIL"
        reason = str(r["reason"]).replace("|", "\\|")
        lines.append(
            f"| `{r['task_id']}` | {r.get('task_type', '-')} | {r['difficulty']} | {status} | "
            f"{r['turns']} | {r['tool_calls']} | {r['duration']}s | {reason} |"
        )
    lines.extend(["", "## By Difficulty", "", "| Difficulty | Passed | Total | Pass Rate |", "|---|---:|---:|---:|"])
    for name, stats in summary.get("by_difficulty", {}).items():
        lines.append(f"| {name} | {stats['passed']} | {stats['total']} | {stats['pass_rate']:.0%} |")
    lines.extend(["", "## By Task Type", "", "| Type | Passed | Total | Pass Rate |", "|---|---:|---:|---:|"])
    for name, stats in summary.get("by_type", {}).items():
        lines.append(f"| {name} | {stats['passed']} | {stats['total']} | {stats['pass_rate']:.0%} |")
    if summary.get("failure_categories"):
        lines.extend(["", "## Failure Categories", "", "| Category | Count |", "|---|---:|"])
        for category, count in summary["failure_categories"].items():
            lines.append(f"| {category} | {count} |")
    return "\n".join(lines) + "\n"


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NZ-Coder Benchmark")
    parser.add_argument("--task", type=str, help="Run a specific task by ID")
    parser.add_argument("--report", action="store_true", help="Show last report")
    parser.add_argument("--list", action="store_true", help="List available benchmark tasks")
    parser.add_argument("--quiet", action="store_true", help="Less output")
    args = parser.parse_args()

    if args.report:
        show_report()
    elif args.list:
        for task in ALL_TASKS:
            print(f"{task.task_id:22s} {task.difficulty:6s} {task.task_type:16s} {task.description[:90]}")
    elif args.task:
        task = TASK_MAP.get(args.task)
        if not task:
            print(f"Unknown task: {args.task}. Available: {list(TASK_MAP.keys())}")
            sys.exit(1)
        result = asyncio.run(run_task(task, verbose=not args.quiet))
        status = "PASS" if result["passed"] else "FAIL"
        print(f"\n[{status}] {result['reason']}")
    else:
        asyncio.run(run_all(verbose=not args.quiet))
