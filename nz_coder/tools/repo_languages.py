"""Conservative standard-library declaration extraction for source languages."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from nz_coder.lsp.servers import language_for_path


@dataclass(frozen=True)
class LanguageSymbol:
    """One declaration extracted without a language-specific parser dependency."""

    kind: str
    name: str
    line: int
    signature: str


@dataclass(frozen=True)
class _DeclarationPattern:
    pattern: re.Pattern[str]
    build: Callable[[re.Match[str]], tuple[str, str]]


def _pattern(
    expression: str,
    kind: str,
    name_group: int = 1,
) -> _DeclarationPattern:
    return _DeclarationPattern(
        re.compile(expression),
        lambda match: (kind, match.group(name_group)),
    )


def _typed(match: re.Match[str]) -> tuple[str, str]:
    return match.group(1).replace(" ", "_"), match.group(2)


def _go_type(match: re.Match[str]) -> tuple[str, str]:
    value = (match.group(2) or "type").strip()
    return value if value in {"struct", "interface"} else "type", match.group(1)


def _go_function(match: re.Match[str]) -> tuple[str, str]:
    return ("method" if match.group(1) else "function"), match.group(2)


def _javascript_type(match: re.Match[str]) -> tuple[str, str]:
    return match.group(1), match.group(2)


def _javascript_variable(match: re.Match[str]) -> tuple[str, str]:
    keyword = match.group(1)
    return ("constant" if keyword == "const" else "variable"), match.group(2)


def _kotlin_type(match: re.Match[str]) -> tuple[str, str]:
    value = match.group(1)
    if value.endswith("class"):
        return "class", match.group(2)
    return value, match.group(2)


def _bash_function(match: re.Match[str]) -> tuple[str, str]:
    return "function", match.group(1) or match.group(2)



_JS_PREFIX = r"^\s*(?:export\s+)?(?:default\s+)?"
_VISIBILITY = r"(?:public|protected|private|internal|static|final|abstract|open|override|suspend)"

_PATTERNS: dict[str, tuple[_DeclarationPattern, ...]] = {
    "typescript": (
        _DeclarationPattern(
            re.compile(
                _JS_PREFIX
                + r"(?:abstract\s+)?(class|interface|enum|type)\s+"
                + r"([A-Za-z_$][\w$]*)"
            ),
            _javascript_type,
        ),
        _pattern(
            _JS_PREFIX
            + r"(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
            "function",
        ),
        _pattern(
            _JS_PREFIX
            + r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
            + r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
            "function",
        ),
        _DeclarationPattern(
            re.compile(_JS_PREFIX + r"(const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?="),
            _javascript_variable,
        ),
    ),
    "go": (
        _DeclarationPattern(
            re.compile(r"^\s*type\s+([A-Za-z_]\w*)\s+(struct|interface)?\b"),
            _go_type,
        ),
        _DeclarationPattern(
            re.compile(
                r"^\s*func\s*(?:(\([^)]*\))\s*)?"
                + r"([A-Za-z_]\w*)\s*\("
            ),
            _go_function,
        ),
    ),
    "rust": (
        _DeclarationPattern(
            re.compile(
                r"^\s*(?:pub(?:\([^)]*\))?\s+)?"
                + r"(struct|enum|trait|type|mod)\s+([A-Za-z_]\w*)"
            ),
            _typed,
        ),
        _pattern(
            r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?"
            + r"fn\s+([A-Za-z_]\w*)\s*(?:<[^>]+>)?\s*\(",
            "function",
        ),
    ),
    "java": (
        _DeclarationPattern(
            re.compile(
                r"^\s*(?:(?:public|protected|private|static|final|abstract|sealed|"
                + r"non-sealed)\s+)*(class|interface|enum|record)\s+"
                + r"([A-Za-z_]\w*)"
            ),
            _typed,
        ),
    ),
    "kotlin": (
        _DeclarationPattern(
            re.compile(
                r"^\s*(?:(?:" + _VISIBILITY + r")\s+)*"
                + r"(?:data\s+|sealed\s+|enum\s+)?"
                + r"(class|interface|object)\s+([A-Za-z_]\w*)"
            ),
            _kotlin_type,
        ),
        _pattern(
            r"^\s*(?:(?:" + _VISIBILITY + r")\s+)*"
            + r"fun\s+(?:<[^>]+>\s*)?([A-Za-z_]\w*)\s*\(",
            "function",
        ),
    ),
    "cpp": (
        _DeclarationPattern(
            re.compile(r"^\s*(class|struct|enum)\s+(?:class\s+)?([A-Za-z_]\w*)"),
            _typed,
        ),
        _pattern(
            r"^\s*(?!(?:if|for|while|switch|catch)\b)"
            + r"(?:[\w:<>~*&]+\s+)+([~A-Za-z_]\w*(?:::\w+)?)"
            + r"\s*\([^;{}]*\)\s*(?:const\s*)?(?:noexcept\s*)?\{",
            "function",
        ),
    ),
    "ruby": (
        _DeclarationPattern(
            re.compile(r"^\s*(class|module)\s+([A-Z]\w*(?:::\w+)*)"),
            _typed,
        ),
        _pattern(r"^\s*def\s+(?:self\.)?([A-Za-z_]\w*[!?=]?)", "function"),
    ),
    "php": (
        _DeclarationPattern(
            re.compile(
                r"^\s*(?:(?:abstract|final|readonly)\s+)*"
                + r"(class|interface|trait|enum)\s+([A-Za-z_]\w*)",
                re.IGNORECASE,
            ),
            _typed,
        ),
        _pattern(
            r"^\s*(?:(?:public|protected|private|static|final|abstract)\s+)*"
            + r"function\s+&?\s*([A-Za-z_]\w*)\s*\(",
            "function",
        ),
    ),
    "lua": (
        _pattern(
            r"^\s*(?:local\s+)?function\s+([A-Za-z_]\w*(?:[.:]\w+)*)\s*\(",
            "function",
        ),
    ),
    "bash": (
        _DeclarationPattern(
            re.compile(
                r"^\s*(?:function\s+([A-Za-z_]\w*)\s*(?:\(\s*\))?"
                + r"|([A-Za-z_]\w*)\s*\(\s*\))\s*\{"
            ),
            _bash_function,
        ),
    ),
}


def supported_language(path: Path) -> str | None:
    """Return the fallback-parser language for a path, if available."""
    language = language_for_path(path)
    return language if language in _PATTERNS else None


def is_supported_source(path: Path) -> bool:
    """Return whether Repo Map can structurally index this source path."""
    language = language_for_path(path)
    return language == "python" or language in _PATTERNS


def _compact(text: str, limit: int = 180) -> str:
    value = " ".join(text.split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def extract_language_symbols(
    path: Path,
    source: str,
) -> tuple[LanguageSymbol, ...]:
    """Extract high-confidence declarations from one non-Python source file."""
    language = supported_language(path)
    if language is None:
        return ()
    patterns = _PATTERNS[language]
    symbols: list[LanguageSymbol] = []
    seen: set[tuple[str, str, int]] = set()
    in_block_comment = False

    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        stripped = raw_line.strip()
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped[2:]:
                in_block_comment = True
            continue
        if (
            not stripped
            or stripped.startswith(("//", "#", "*"))
        ):
            continue
        for declaration in patterns:
            match = declaration.pattern.match(raw_line)
            if match is None:
                continue
            kind, name = declaration.build(match)
            key = (kind.casefold(), name.casefold(), line_number)
            if key in seen:
                break
            seen.add(key)
            symbols.append(LanguageSymbol(
                kind=kind.casefold(),
                name=name,
                line=line_number,
                signature=_compact(stripped),
            ))
            break
    return tuple(symbols)
