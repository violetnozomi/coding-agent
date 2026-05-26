"""Task and project policy helpers for general coding-agent behavior.

这些函数把原先散落在 SWE-bench 策略里的判断集中起来：文件类型、
测试文件识别、任务模式识别和 broad test runner 判断。保持无外部依赖，
供 tools/runtime/subagent 共享。
"""
from __future__ import annotations

import re
from pathlib import Path


_TEST_DIR_NAMES = {"test", "tests", "__tests__", "spec", "specs"}
_TEST_SUFFIXES = (
    "_test.py", "_test.go", "_test.rb", "_spec.rb",
    ".test.js", ".test.jsx", ".test.ts", ".test.tsx",
    ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx",
    ".test.mjs", ".spec.mjs", ".test.cjs", ".spec.cjs",
)
_SOURCE_LANG_BY_EXT = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
}


def normalize_path(path: str) -> str:
    """归一化为 forward-slash 路径，便于跨平台匹配。"""
    return (path or "").replace("\\", "/")


def language_for_path(path: str) -> str:
    """返回路径对应的主语言，未知则返回 ``other``。"""
    suffix = Path(normalize_path(path)).suffix.lower()
    return _SOURCE_LANG_BY_EXT.get(suffix, "other")


def is_source_file(path: str) -> bool:
    """True 表示路径看起来是可编译/可检查的源码文件。"""
    return language_for_path(path) != "other"


def is_test_file(path: str) -> bool:
    """语言无关的测试文件识别。

    覆盖 Python、JS/TS、Go、Ruby 及常见 tests/spec/__tests__ 目录。
    """
    p = normalize_path(path)
    if not p:
        return False
    parts = [part.lower() for part in p.split("/") if part]
    name = parts[-1] if parts else ""
    if any(part in _TEST_DIR_NAMES for part in parts[:-1]):
        return True
    return name.startswith("test_") or name.endswith(_TEST_SUFFIXES)


def detect_task_mode(text: str) -> str:
    """从用户文本粗略识别任务模式。

    匹配优先级是 test > refactor > feature > bugfix > discuss > general。
    例如 "add a test for the login endpoint" 会归为 test，因为测试意图
    比 endpoint 创建意图更具体。
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return "general"
    test_markers = ("add test", "unit test", "pytest", "coverage", "测试", "单元测试")
    refactor_markers = ("refactor", "rename", "migrate", "migration", "重构", "迁移", "改名")
    feature_markers = (
        "add ", "implement", "create", "new ", "endpoint", "feature",
        "scaffold", "初始化", "新建", "创建", "实现", "添加", "加一个",
    )
    bug_markers = (
        "fix", "bug", "traceback", "error", "failing", "failed", "regression",
        "报错", "失败", "修复", "问题", "异常",
    )
    discuss_markers = (
        "explain", "why", "how should", "design", "proposal", "方案", "讨论",
        "解释", "为什么", "怎么设计", "如何设计",
    )

    if any(marker in lowered for marker in test_markers):
        return "test"
    if any(marker in lowered for marker in refactor_markers):
        return "refactor"
    if any(marker in lowered for marker in feature_markers):
        return "feature"
    if any(marker in lowered for marker in bug_markers):
        return "bugfix"
    if any(marker in lowered for marker in discuss_markers) or "?" in lowered or "？" in lowered:
        return "discuss"
    return "general"


def task_wants_tests(text: str) -> bool:
    """True 表示用户明确要求测试相关工作。"""
    lowered = (text or "").lower()
    markers = (
        "test", "tests", "testing", "pytest", "unittest", "coverage", "spec",
        "测试", "单元测试", "集成测试", "覆盖率",
    )
    return any(marker in lowered for marker in markers)


def estimate_text_complexity(text: str) -> str:
    """从用户任务文本预估复杂度，用于 planning 触发判断。

    不依赖 RuntimeState（planning 前没有 diff/edit 信息）。返回
    ``simple`` / ``moderate`` / ``complex``。
    """
    if not text or len(text.strip()) < 20:
        return "simple"

    lowered = text.lower()
    score = 0
    file_refs = re.findall(r"[\w/\-]+\.(?:py|js|ts|go|rs|java|rb)\b", text)
    if len(set(file_refs)) >= 3:
        score += 2
    elif len(set(file_refs)) >= 1:
        score += 1

    if re.search(r"(?:^|\n)\s*(?:\d+[.)]\s|[-*]\s)", text, re.MULTILINE):
        score += 2
    if any(w in lowered for w in ("then ", "after that", "finally", "第一步", "然后", "最后")):
        score += 1

    if len(text) > 800:
        score += 1
    if len(text) > 2000:
        score += 1

    if any(w in lowered for w in (
        "across", "multiple files", "refactor", "migrate", "rename all",
        "每个文件", "所有模块", "批量",
    )):
        score += 2

    if score >= 4:
        return "complex"
    if score >= 2:
        return "moderate"
    return "simple"


def is_exact_test_command(command: str) -> bool:
    """True 表示命令运行的是聚焦/精确测试。"""
    cmd = " ".join((command or "").strip().lower().split())
    if not cmd:
        return False
    if "::" in cmd:
        return True
    if re.search(r"\s-run\s+\w+", cmd):
        return True
    if " --test " in cmd or " --filter " in cmd:
        return True
    if re.search(r"\s\S+\.(?:py|js|jsx|ts|tsx|go|rs|rb)\b", cmd):
        return True
    return False


def is_broad_test_command(command: str) -> bool:
    """判断命令是否是 broad test runner。"""
    cmd = " ".join((command or "").strip().lower().split())
    if not cmd:
        return False

    runner_markers = (
        "pytest", "tox", "nox", "runtests.py", "manage.py test", "unittest",
        "npm test", "npm run test", "pnpm test", "pnpm run test",
        "yarn test", "yarn run test", "vitest", "jest",
        "go test", "cargo test", "mvn test", "gradle test", "./gradlew test",
        "make test",
    )
    if not any(marker in cmd for marker in runner_markers):
        return False
    if is_exact_test_command(cmd):
        return False
    if "go test" in cmd and " ./..." not in cmd and " -run " in cmd:
        return False
    return True
