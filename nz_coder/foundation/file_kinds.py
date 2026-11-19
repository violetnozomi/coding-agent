"""共享的路径类型判断；只提供候选分类，不证明文件内容或执行结果。"""
from pathlib import Path

TEST_DIR_NAMES = {"test", "tests", "testing", "__tests__", "spec", "specs"}
_TEST_SUFFIXES = (
    "_test.py", "_test.go", "_test.rb", "_spec.rb",
    ".test.js", ".test.jsx", ".test.ts", ".test.tsx",
    ".spec.js", ".spec.jsx", ".spec.ts", ".spec.tsx",
    ".test.mjs", ".spec.mjs", ".test.cjs", ".spec.cjs",
)


def is_test_file(path: str) -> bool:
    """识别测试路径，内容充分性仍需独立检查。"""
    normalized = (path or "").replace("\\", "/")
    parts = [part.lower() for part in normalized.split("/") if part]
    name = parts[-1] if parts else ""
    return any(part in TEST_DIR_NAMES for part in parts[:-1]) or name.startswith("test_") or name.endswith(_TEST_SUFFIXES)


def is_python_cache_path(path: str) -> bool:
    """缓存可以是工作区变化，不能作为模型编写的测试或文档。"""
    normalized = path.replace("\\", "/").casefold()
    return "__pycache__" in normalized.split("/") or Path(normalized).suffix in {".pyc", ".pyo"}


def is_documentation_file(path: str) -> bool:
    """识别文档范围，保留现有路径分类规则。"""
    normalized = (path or "").replace("\\", "/")
    parts = [part.casefold() for part in normalized.split("/") if part]
    return bool(normalized and (
        Path(normalized).suffix.casefold() in {".md", ".rst", ".txt"}
        or any(part in {"doc", "docs"} for part in parts[:-1])
    ))


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


def language_for_path(path: str) -> str:
    """返回源码语言；配置和未知文件返回 other。"""
    return _SOURCE_LANG_BY_EXT.get(Path(str(path or "").replace("\\", "/")).suffix.lower(), "other")


def is_source_file(path: str) -> bool:
    return language_for_path(path) != "other"
