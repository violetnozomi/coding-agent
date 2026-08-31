"""Language analyzers used by the persistent repository index.

Parsing is deliberately separated from persistence and cross-file resolution.
Analyzers describe declarations, imports, references, and raw call sites.  The
index resolves those raw targets after the affected files have been committed.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol

class CapabilityTier(str, Enum):
    AST_NATIVE = "ast-native"
    TREE_SITTER = "tree-sitter"
    LSP_AUGMENTED = "lsp-augmented"
    LEXICAL_FALLBACK = "lexical-fallback"


@dataclass(frozen=True)
class SymbolRecord:
    symbol_id: str
    name: str
    qualified_name: str
    kind: str
    file_path: str
    module_id: str
    language: str
    line: int
    end_line: int
    signature: str | None
    exported: bool | None
    confidence: float
    source: str
    capability_tier: str


@dataclass(frozen=True)
class ImportRecord:
    file_path: str
    module: str
    binding: str
    imported_name: str | None
    alias: str | None
    line: int
    kind: str
    confidence: float


@dataclass(frozen=True)
class ReferenceRecord:
    source_file: str
    source_symbol_id: str | None
    raw_name: str
    qualifier: str
    line: int
    column: int
    context: str
    confidence: float
    source: str


@dataclass(frozen=True)
class RawCallRecord:
    caller_symbol_id: str
    caller_name: str
    raw_name: str
    qualifier: str
    call_site_file: str
    line: int
    confidence: float
    source: str


@dataclass(frozen=True)
class AnalysisResult:
    language: str
    capability_tier: str
    confidence: float
    source: str
    symbols: tuple[SymbolRecord, ...]
    imports: tuple[ImportRecord, ...]
    references: tuple[ReferenceRecord, ...]
    calls: tuple[RawCallRecord, ...]
    parse_error: str = ""


class LanguageAnalyzer(Protocol):
    """Parser boundary implemented by every language intelligence tier."""

    languages: frozenset[str]
    capability_tier: CapabilityTier

    def available(self) -> bool: ...

    def analyze_file(
        self, *, path: Path, relative: str, source: str, language: str,
    ) -> AnalysisResult: ...


def module_name_for_path(relative: str) -> str:
    path = Path(relative)
    parts = list(path.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts) or path.stem


def module_id_for_path(
    relative: str, *, boundary_roots: tuple[str, ...] = (),
) -> str:
    """Return a stable package-area identity while retaining root-file isolation.

    Source-file identity remains in ``SymbolId``.  A module is the smallest useful
    repository area: a top-level package, or one named workspace package below a
    conventional monorepo container.  Root files stay independent so unrelated
    entry/config files are not collapsed into one synthetic module.
    """
    normalized = relative.replace(chr(92), "/").lstrip("./")
    parts = Path(normalized).parts
    if len(parts) <= 1:
        return f"module:{normalized}"
    matching_roots = [
        root for root in boundary_roots
        if normalized == root or normalized.startswith(root.rstrip("/") + "/")
    ]
    if matching_roots:
        return f"module:{max(matching_roots, key=lambda item: (item.count('/'), len(item)))}"
    if parts[0] in {
        "apps", "cmd", "crates", "libs", "modules", "packages", "plugins",
        "services", "tools",
    } and len(parts) >= 3:
        root = "/".join(parts[:2])
    elif parts[0] == "src" and len(parts) >= 3:
        # ``src/auth`` and ``src/payment`` are normally separate package areas;
        # collapsing both into module:src makes impact and context too coarse.
        root = "/".join(parts[:2])
    else:
        root = parts[0]
    return f"module:{root}"


def discover_module_boundaries(workspace: Path) -> tuple[str, ...]:
    """Discover nested package roots from manifests and workspace metadata.

    Root manifests describe the whole repository and are intentionally not used
    to collapse all source into one module. Nested manifests and declared JS
    workspace packages are stable, parser-independent boundaries.
    """
    root = Path(workspace).resolve()
    ignored = {
        ".git", ".hg", ".nz-coder", ".pytest_cache", ".ruff_cache",
        ".venv", "__pycache__", "build", "dist", "node_modules", "venv",
    }
    markers = {"Cargo.toml", "go.mod", "package.json", "pyproject.toml"}
    boundaries: set[str] = set()
    for directory, directories, files in __import__("os").walk(root, followlinks=False):
        directories[:] = sorted(name for name in directories if name not in ignored)
        current = Path(directory)
        if current != root and markers.intersection(files):
            boundaries.add(current.relative_to(root).as_posix())
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        raw_workspaces = payload.get("workspaces", ()) if isinstance(payload, dict) else ()
        if isinstance(raw_workspaces, dict):
            raw_workspaces = raw_workspaces.get("packages", ())
        if isinstance(raw_workspaces, list):
            for pattern in raw_workspaces:
                if not isinstance(pattern, str) or pattern.startswith(("/", "..")):
                    continue
                for match in root.glob(pattern):
                    if match.is_dir():
                        boundaries.add(match.relative_to(root).as_posix())
    return tuple(sorted(boundaries))


def symbol_id_for(
    relative: str, qualified_name: str, kind: str, *, discriminator: str = "",
) -> str:
    suffix = f"@{discriminator}" if discriminator else ""
    return f"symbol:{relative}::{qualified_name}::{kind}{suffix}"


def _compact(text: str, limit: int = 240) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[: limit - 3].rstrip() + "..."


def _python_signature(node: ast.AST) -> str | None:
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(base) for base in node.bases]
        suffix = f"({', '.join(bases)})" if bases else ""
        return _compact(f"class {node.name}{suffix}")
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        signature = f"{prefix} {node.name}({ast.unparse(node.args)})"
        if node.returns is not None:
            signature += f" -> {ast.unparse(node.returns)}"
        return _compact(signature)
    return None


def _attribute_parts(node: ast.AST) -> list[str] | None:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        left = _attribute_parts(node.value)
        return [*left, node.attr] if left else [node.attr]
    return None


class PythonAstAnalyzer:
    languages = frozenset({"python"})
    capability_tier = CapabilityTier.AST_NATIVE

    def available(self) -> bool:
        return True

    def analyze_file(
        self, *, path: Path, relative: str, source: str, language: str,
    ) -> AnalysisResult:
        tree = ast.parse(source, filename=str(path))
        module_name = module_name_for_path(relative)
        module_id = module_id_for_path(relative)
        lines = source.splitlines()
        symbols: list[SymbolRecord] = []
        node_symbols: dict[ast.AST, SymbolRecord] = {}
        owner_class: dict[ast.AST, str] = {}
        symbol_occurrences: dict[str, int] = {}

        def collect(body: list[ast.stmt], prefix: str = "", class_name: str = "") -> None:
            for node in body:
                if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                local_name = f"{prefix}.{node.name}" if prefix else node.name
                qualified = f"{module_name}.{local_name}" if module_name else local_name
                if isinstance(node, ast.ClassDef):
                    kind = "class"
                elif class_name:
                    kind = "async method" if isinstance(node, ast.AsyncFunctionDef) else "method"
                else:
                    kind = "async function" if isinstance(node, ast.AsyncFunctionDef) else "function"
                base_symbol_id = symbol_id_for(relative, qualified, kind)
                occurrence = symbol_occurrences.get(base_symbol_id, 0) + 1
                symbol_occurrences[base_symbol_id] = occurrence
                record = SymbolRecord(
                    symbol_id=(
                        base_symbol_id
                        if occurrence == 1
                        else symbol_id_for(
                            relative,
                            qualified,
                            kind,
                            discriminator=f"duplicate-{occurrence}",
                        )
                    ),
                    name=node.name,
                    qualified_name=qualified,
                    kind=kind,
                    file_path=relative,
                    module_id=module_id,
                    language="python",
                    line=int(getattr(node, "lineno", 1)),
                    end_line=int(getattr(node, "end_lineno", getattr(node, "lineno", 1))),
                    signature=_python_signature(node),
                    exported=not node.name.startswith("_"),
                    confidence=0.99,
                    source="python-ast",
                    capability_tier=self.capability_tier.value,
                )
                symbols.append(record)
                node_symbols[node] = record
                if class_name:
                    owner_class[node] = class_name
                if isinstance(node, ast.ClassDef):
                    collect(node.body, local_name, local_name)
                else:
                    collect(node.body, local_name, class_name)

        collect(tree.body)

        imports: list[ImportRecord] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for item in node.names:
                    binding = item.asname or item.name.split(".", 1)[0]
                    imports.append(ImportRecord(
                        relative, item.name, binding, None, item.asname,
                        int(node.lineno), "import", 1.0,
                    ))
            elif isinstance(node, ast.ImportFrom):
                module = "." * int(node.level or 0) + (node.module or "")
                for item in node.names:
                    binding = item.asname or item.name
                    imports.append(ImportRecord(
                        relative, module, binding, item.name, item.asname,
                        int(node.lineno), "from-import", 1.0,
                    ))

        references: dict[tuple[str, str, int, int], ReferenceRecord] = {}

        class ReferenceVisitor(ast.NodeVisitor):
            def __init__(self) -> None:
                self.owner: SymbolRecord | None = None

            def _owned_visit(self, node: ast.AST) -> None:
                previous = self.owner
                self.owner = node_symbols.get(node, previous)
                self.generic_visit(node)
                self.owner = previous

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._owned_visit(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._owned_visit(node)

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self._owned_visit(node)

            def _record(self, node: ast.AST, parts: list[str]) -> None:
                if not parts or not hasattr(node, "lineno"):
                    return
                line = int(node.lineno)
                column = int(getattr(node, "col_offset", 0))
                owner_id = self.owner.symbol_id if self.owner else ""
                raw_name = parts[-1]
                key = (owner_id, raw_name, line, column)
                references[key] = ReferenceRecord(
                    relative, owner_id or None, raw_name, ".".join(parts[:-1]),
                    line, column,
                    _compact(lines[line - 1] if line <= len(lines) else ""),
                    0.98, "python-ast",
                )

            def visit_Name(self, node: ast.Name) -> None:
                if isinstance(node.ctx, ast.Load):
                    self._record(node, [node.id])

            def visit_Attribute(self, node: ast.Attribute) -> None:
                if isinstance(node.ctx, ast.Load):
                    self._record(node, _attribute_parts(node) or [node.attr])
                self.visit(node.value)

        ReferenceVisitor().visit(tree)

        calls: list[RawCallRecord] = []

        class DirectCallVisitor(ast.NodeVisitor):
            def __init__(self, root: ast.AST) -> None:
                self.root = root
                self.items: list[ast.Call] = []

            def visit_Call(self, node: ast.Call) -> None:
                self.items.append(node)
                self.generic_visit(node)

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                if node is self.root:
                    self.generic_visit(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                if node is self.root:
                    self.generic_visit(node)

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                return None

            def visit_Lambda(self, node: ast.Lambda) -> None:
                return None

        for node, caller in node_symbols.items():
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            visitor = DirectCallVisitor(node)
            visitor.visit(node)
            for call in visitor.items:
                parts = _attribute_parts(call.func)
                if not parts:
                    continue
                calls.append(RawCallRecord(
                    caller_symbol_id=caller.symbol_id,
                    caller_name=caller.name,
                    raw_name=parts[-1],
                    qualifier=".".join(parts[:-1]),
                    call_site_file=relative,
                    line=int(call.lineno),
                    confidence=0.98,
                    source="python-ast",
                ))

        return AnalysisResult(
            "python", self.capability_tier.value, 0.99, "python-ast",
            tuple(symbols), tuple(imports), tuple(references.values()), tuple(calls),
        )


_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_$][\w$]*\b")
_CALL_RE = re.compile(r"\b((?:[A-Za-z_$][\w$]*\.)*)([A-Za-z_$][\w$]*)\s*\(")
_CALL_KEYWORDS = frozenset({
    "if", "for", "while", "switch", "catch", "return", "sizeof", "typeof",
})


@dataclass(frozen=True)
class _LexicalSymbol:
    kind: str
    name: str
    line: int
    signature: str


_DECLARATIONS: dict[str, tuple[tuple[str, re.Pattern[str]], ...]] = {
    "typescript": (
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")),
    ),
    "javascript": (
        ("class", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_$][\w$]*)")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")),
        ("function", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>")),
    ),
    "go": (
        ("type", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\b")),
        ("function", re.compile(r"^\s*func\s*(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")),
    ),
    "rust": (
        ("type", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type|mod)\s+([A-Za-z_]\w*)")),
        ("function", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*(?:<[^>]+>)?\s*\(")),
    ),
    "java": (
        ("class", re.compile(r"^\s*(?:(?:public|protected|private|static|final|abstract)\s+)*(?:class|interface|enum|record)\s+([A-Za-z_]\w*)")),
    ),
    "cpp": (
        ("type", re.compile(r"^\s*(?:class|struct|enum)\s+(?:class\s+)?([A-Za-z_]\w*)")),
    ),
}


def _extract_language_symbols(source: str, language: str) -> tuple[_LexicalSymbol, ...]:
    result: list[_LexicalSymbol] = []
    for line, raw in enumerate(source.splitlines(), start=1):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("//", "#", "*")):
            continue
        for kind, pattern in _DECLARATIONS.get(language, ()):
            match = pattern.match(raw)
            if match:
                resolved_kind = kind
                if language == "go" and kind == "type":
                    typed = re.match(r"^\s*type\s+[A-Za-z_]\w*\s+(struct|interface)\b", raw)
                    if typed:
                        resolved_kind = typed.group(1)
                elif language == "rust" and kind == "type":
                    typed = re.match(
                        r"^\s*(?:pub(?:\([^)]*\))?\s+)?"
                        r"(struct|enum|trait|type|mod)\s+", raw,
                    )
                    if typed:
                        resolved_kind = typed.group(1)
                result.append(_LexicalSymbol(
                    resolved_kind, match.group(1), line, _compact(stripped, 180),
                ))
                break
    return tuple(result)


class TreeSitterAnalyzer:
    """Optional TS/JS/Go parser using official py-tree-sitter grammar wheels."""

    languages = frozenset({"typescript", "javascript", "go"})
    capability_tier = CapabilityTier.TREE_SITTER

    def __init__(self) -> None:
        self._parsers: dict[str, object] = {}
        self._errors: dict[str, str] = {}
        try:
            from tree_sitter import Language, Parser
        except (ImportError, OSError, TypeError, ValueError) as exc:
            detail = f"{type(exc).__name__}: {exc}"
            self._errors = {language: detail for language in self.languages}
            return

        loaders = {
            "javascript": lambda: __import__("tree_sitter_javascript").language(),
            "typescript": lambda: __import__(
                "tree_sitter_typescript"
            ).language_typescript(),
            "go": lambda: __import__("tree_sitter_go").language(),
        }
        for language, loader in loaders.items():
            try:
                self._parsers[language] = Parser(Language(loader()))
            except (ImportError, OSError, TypeError, ValueError) as exc:
                self._errors[language] = f"{type(exc).__name__}: {exc}"

    def available(self) -> bool:
        return bool(self._parsers)

    def available_for(self, language: str) -> bool:
        return language in self._parsers

    def capability_probe(self) -> dict[str, dict[str, str | bool]]:
        return {
            language: {
                "available": language in self._parsers,
                "capability_tier": (
                    self.capability_tier.value
                    if language in self._parsers
                    else CapabilityTier.LEXICAL_FALLBACK.value
                ),
                "detail": self._errors.get(language, "parser importable"),
            }
            for language in sorted(self.languages)
        }

    @staticmethod
    def _text(source_bytes: bytes, node: object | None) -> str:
        if node is None:
            return ""
        return source_bytes[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def analyze_file(
        self, *, path: Path, relative: str, source: str, language: str,
    ) -> AnalysisResult:
        parser = self._parsers.get(language)
        if parser is None:
            raise RuntimeError(f"tree-sitter parser unavailable for {language}")
        content = source.encode("utf-8")
        tree = parser.parse(content)
        module_name = module_name_for_path(relative)
        module_id = module_id_for_path(relative)
        lines = source.splitlines()
        symbols: list[SymbolRecord] = []
        symbol_nodes: list[tuple[object, object, SymbolRecord]] = []
        imports: list[ImportRecord] = []
        symbol_occurrences: dict[str, int] = {}

        def add_symbol(node: object, name_node: object | None, kind: str, owner: str = "") -> None:
            name = self._text(content, name_node).strip()
            if not name:
                return
            local = f"{owner}.{name}" if owner else name
            qualified = f"{module_name}.{local}" if module_name else local
            line = int(node.start_point[0]) + 1
            signature = lines[line - 1].strip() if line <= len(lines) else None
            parent_text = self._text(content, getattr(node, "parent", None))[:80]
            exported = parent_text.lstrip().startswith("export ") or signature.lstrip().startswith("export ")
            base_symbol_id = symbol_id_for(relative, qualified, kind)
            occurrence = symbol_occurrences.get(base_symbol_id, 0) + 1
            symbol_occurrences[base_symbol_id] = occurrence
            record = SymbolRecord(
                (
                    base_symbol_id
                    if occurrence == 1
                    else symbol_id_for(
                        relative,
                        qualified,
                        kind,
                        discriminator=f"duplicate-{occurrence}",
                    )
                ), name, qualified, kind,
                relative, module_id, language, line, int(node.end_point[0]) + 1,
                _compact(signature or "") or None, exported, 0.92,
                f"tree-sitter-{language}", self.capability_tier.value,
            )
            symbols.append(record)
            if kind in {"function", "method"}:
                body = node.child_by_field_name("body") or node
                symbol_nodes.append((node, body, record))

        def visit(node: object, owner: str = "") -> None:
            node_type = node.type
            if node_type in {"function_declaration", "generator_function_declaration"}:
                add_symbol(node, node.child_by_field_name("name"), "function", owner)
            elif language == "go" and node_type == "method_declaration":
                receiver = self._text(content, node.child_by_field_name("receiver"))
                receiver_names = re.findall(r"[A-Za-z_]\w*", receiver)
                receiver_type = receiver_names[-1] if receiver_names else ""
                add_symbol(node, node.child_by_field_name("name"), "method", receiver_type)
            elif language == "go" and node_type == "type_declaration":
                for child in node.children:
                    if child.type != "type_spec":
                        continue
                    value = child.child_by_field_name("type")
                    kind = "interface" if value is not None and value.type == "interface_type" else "struct" if value is not None and value.type == "struct_type" else "type"
                    add_symbol(child, child.child_by_field_name("name"), kind, owner)
            elif node_type == "class_declaration":
                name_node = node.child_by_field_name("name")
                class_name = self._text(content, name_node).strip()
                add_symbol(node, name_node, "class", owner)
                for child in node.children:
                    visit(child, f"{owner}.{class_name}".strip("."))
                return
            elif node_type in {"method_definition", "method_signature"}:
                add_symbol(node, node.child_by_field_name("name"), "method", owner)
            elif node_type == "variable_declarator":
                value = node.child_by_field_name("value")
                if value is not None and value.type in {"arrow_function", "function_expression"}:
                    add_symbol(node, node.child_by_field_name("name"), "function", owner)
                if language == "javascript" and value is not None:
                    raw_value = self._text(content, value)
                    required = re.search(r"\brequire\s*\(\s*['\"]([^'\"]+)['\"]\s*\)", raw_value)
                    if required:
                        module = required.group(1)
                        raw_name = self._text(content, node.child_by_field_name("name")).strip()
                        line = int(node.start_point[0]) + 1
                        if re.fullmatch(r"[A-Za-z_$][\w$]*", raw_name):
                            member = re.search(r"\.([A-Za-z_$][\w$]*)\s*$", raw_value)
                            imports.append(ImportRecord(
                                relative, module, raw_name,
                                member.group(1) if member else None,
                                raw_name if member else None, line,
                                "from-import" if member else "import", 0.92,
                            ))
                        elif raw_name.startswith("{"):
                            for part in raw_name.strip("{} ").split(","):
                                values = [item.strip() for item in re.split(r"\s*:\s*", part)]
                                if values and values[0]:
                                    imports.append(ImportRecord(
                                        relative, module, values[-1], values[0],
                                        values[-1] if len(values) > 1 else None,
                                        line, "from-import", 0.9,
                                    ))
            elif node_type == "import_statement":
                raw = self._text(content, node)
                module_match = re.search(r"\bfrom\s+['\"]([^'\"]+)|\bimport\s*['\"]([^'\"]+)", raw)
                module = next((item for item in (module_match.groups() if module_match else ()) if item), "")
                if module:
                    namespace = re.search(r"\*\s+as\s+([A-Za-z_$][\w$]*)", raw)
                    default = re.match(r"\s*import\s+([A-Za-z_$][\w$]*)", raw)
                    named = re.search(r"\{([^}]+)\}", raw)
                    if namespace:
                        imports.append(ImportRecord(relative, module, namespace.group(1), None, namespace.group(1), int(node.start_point[0]) + 1, "import", 0.95))
                    if default and not raw.lstrip().startswith("import {"):
                        imports.append(ImportRecord(relative, module, default.group(1), "default", None, int(node.start_point[0]) + 1, "from-import", 0.9))
                    if named:
                        for part in named.group(1).split(","):
                            values = re.split(r"\s+as\s+", part.strip())
                            if values and values[0]:
                                binding = values[-1].strip()
                                imports.append(ImportRecord(relative, module, binding, values[0].strip(), binding if len(values) > 1 else None, int(node.start_point[0]) + 1, "from-import", 0.95))
            elif language == "go" and node_type == "import_spec":
                raw_path = self._text(content, node.child_by_field_name("path")).strip("\"`")
                if raw_path:
                    alias_node = node.child_by_field_name("name")
                    alias = self._text(content, alias_node).strip() or None
                    binding = alias or raw_path.rstrip("/").rsplit("/", 1)[-1]
                    imports.append(ImportRecord(
                        relative, raw_path, binding, None, alias,
                        int(node.start_point[0]) + 1, "import", 0.95,
                    ))
            elif language in {"typescript", "javascript"} and node_type == "export_statement":
                raw = self._text(content, node)
                module_match = re.search(r"\bfrom\s+['\"]([^'\"]+)", raw)
                named = re.search(r"\{([^}]+)\}", raw)
                if module_match and named:
                    for part in named.group(1).split(","):
                        values = re.split(r"\s+as\s+", part.strip())
                        if values and values[0]:
                            imports.append(ImportRecord(
                                relative, module_match.group(1), values[-1].strip(),
                                values[0].strip(),
                                values[-1].strip() if len(values) > 1 else None,
                                int(node.start_point[0]) + 1, "re-export", 0.94,
                            ))
            for child in node.children:
                visit(child, owner)

        visit(tree.root_node)

        references: list[ReferenceRecord] = []
        calls: list[RawCallRecord] = []
        def walk(node: object, callback) -> None:
            callback(node)
            for child in node.children:
                walk(child, callback)

        for _declaration, body, caller in symbol_nodes:
            def collect_call(node: object) -> None:
                if node.type not in {"call_expression", "new_expression"}:
                    return
                function = node.child_by_field_name("function") or node.child_by_field_name("constructor")
                raw = self._text(content, function).strip()
                match = re.search(r"([A-Za-z_$][\w$]*)$", raw)
                if not match or match.group(1) in _CALL_KEYWORDS:
                    return
                calls.append(RawCallRecord(
                    caller.symbol_id, caller.name, match.group(1),
                    raw[:match.start()].rstrip("."), relative,
                    int(node.start_point[0]) + 1, 0.9, f"tree-sitter-{language}",
                ))
            walk(body, collect_call)

        def collect_reference(node: object) -> None:
            if node.type not in {
                "field_identifier", "identifier", "package_identifier",
                "property_identifier", "type_identifier",
            }:
                return
            parent = getattr(node, "parent", None)
            if parent is not None and parent.type in {
                "class_declaration", "function_declaration",
                "generator_function_declaration", "method_declaration",
                "method_definition", "method_signature", "type_spec",
                "variable_declarator",
            } and parent.child_by_field_name("name") == node:
                return
            name = self._text(content, node)
            line = int(node.start_point[0]) + 1
            owner = None
            owner_width = None
            for declaration, body, candidate in symbol_nodes:
                if body.start_byte <= node.start_byte and body.end_byte >= node.end_byte:
                    width = body.end_byte - body.start_byte
                    if owner_width is None or width < owner_width:
                        owner, owner_width = candidate, width
            qualifier = ""
            if parent is not None and parent.type in {
                "member_expression", "selector_expression",
            }:
                parent_text = self._text(content, parent)
                if parent_text.endswith(name):
                    qualifier = parent_text[:-len(name)].rstrip(".")
            references.append(ReferenceRecord(
                relative, owner.symbol_id if owner else None, name, qualifier,
                line, int(node.start_point[1]),
                _compact(lines[line - 1] if line <= len(lines) else ""),
                0.9, f"tree-sitter-{language}",
            ))

        walk(tree.root_node, collect_reference)
        return AnalysisResult(
            language, self.capability_tier.value, 0.92, f"tree-sitter-{language}",
            tuple(symbols), tuple(imports), tuple(references), tuple(calls),
        )


class LexicalFallbackAnalyzer:
    languages = frozenset({
        "typescript", "javascript", "go", "rust", "java", "cpp", "unknown",
    })
    capability_tier = CapabilityTier.LEXICAL_FALLBACK

    def available(self) -> bool:
        return True

    def analyze_file(
        self, *, path: Path, relative: str, source: str, language: str,
    ) -> AnalysisResult:
        module_name = module_name_for_path(relative)
        module_id = module_id_for_path(relative)
        extracted = _extract_language_symbols(source, language)
        symbols: list[SymbolRecord] = []
        symbol_occurrences: dict[str, int] = {}
        for item in extracted:
            qualified = f"{module_name}.{item.name}" if module_name else item.name
            signature = item.signature or None
            exported: bool | None
            if language == "go":
                exported = bool(item.name[:1].isupper())
            elif language in {"typescript", "javascript"}:
                exported = bool(signature and signature.lstrip().startswith("export "))
            elif language == "rust":
                exported = bool(signature and signature.lstrip().startswith("pub "))
            else:
                exported = None
            base_symbol_id = symbol_id_for(relative, qualified, item.kind)
            occurrence = symbol_occurrences.get(base_symbol_id, 0) + 1
            symbol_occurrences[base_symbol_id] = occurrence
            symbols.append(SymbolRecord(
                (
                    base_symbol_id
                    if occurrence == 1
                    else symbol_id_for(
                        relative,
                        qualified,
                        item.kind,
                        discriminator=f"duplicate-{occurrence}",
                    )
                ), item.name, qualified,
                item.kind, relative, module_id, language, item.line, item.line,
                signature, exported, 0.55, f"lexical-{language}",
                self.capability_tier.value,
            ))

        references: list[ReferenceRecord] = []
        calls: list[RawCallRecord] = []
        imports: list[ImportRecord] = []
        if language in {"typescript", "javascript"}:
            raw_imports = re.findall(
                r"(?:from\s+|require\s*\(\s*)['\"]([^'\"]+)", source,
            )
            raw_imports += re.findall(
                r"^\s*import\s*['\"]([^'\"]+)", source, re.MULTILINE,
            )
        elif language == "go":
            raw_imports = re.findall(r"['\"]([^'\"]+)['\"]", source)
        elif language == "rust":
            pairs = re.findall(
                r"^\s*(?:mod\s+([A-Za-z_]\w*)|use\s+(?:crate::)?([A-Za-z_]\w*))",
                source, re.MULTILINE,
            )
            raw_imports = [left or right for left, right in pairs]
        else:
            raw_imports = []
        for imported_module in dict.fromkeys(raw_imports):
            binding = imported_module.rstrip("/").split("/")[-1].split(".")[-1]
            imports.append(ImportRecord(
                relative, imported_module, binding, None, None, 1, "import", 0.5,
            ))
        functions = sorted(
            (item for item in symbols if "function" in item.kind or "method" in item.kind),
            key=lambda item: item.line,
        )
        lines = source.splitlines()
        for line_number, raw in enumerate(lines, start=1):
            stripped = raw.strip()
            if not stripped or stripped.startswith(("//", "#")):
                continue
            caller = next(
                (item for item in reversed(functions) if item.line <= line_number), None,
            )
            for match in _IDENTIFIER_RE.finditer(raw):
                references.append(ReferenceRecord(
                    relative, caller.symbol_id if caller else None, match.group(0), "",
                    line_number, match.start(), _compact(raw), 0.4,
                    f"lexical-{language}",
                ))
            if caller is None:
                continue
            for match in _CALL_RE.finditer(raw):
                name = match.group(2)
                prefix = raw[:match.start(1)]
                if name in _CALL_KEYWORDS or re.search(r"(?:function|func|fn|def)\s+$", prefix):
                    continue
                calls.append(RawCallRecord(
                    caller.symbol_id, caller.name, name,
                    match.group(1).rstrip("."), relative, line_number,
                    0.45, f"lexical-{language}",
                ))
        return AnalysisResult(
            language, self.capability_tier.value, 0.55, f"lexical-{language}",
            tuple(symbols), tuple(imports), tuple(references), tuple(calls),
        )


class AnalyzerRegistry:
    """Select the strongest available analyzer without hiding fallback use."""

    def __init__(self, analyzers: tuple[LanguageAnalyzer, ...] | None = None) -> None:
        self.analyzers = analyzers or (
            PythonAstAnalyzer(), TreeSitterAnalyzer(), LexicalFallbackAnalyzer(),
        )

    def analyzer_for(self, language: str) -> LanguageAnalyzer:
        for analyzer in self.analyzers:
            available_for = getattr(analyzer, "available_for", None)
            available = (
                bool(available_for(language)) if callable(available_for)
                else analyzer.available()
            )
            if language in analyzer.languages and available:
                return analyzer
        return LexicalFallbackAnalyzer()

    def capability_probe(self) -> dict[str, dict[str, str | bool]]:
        result: dict[str, dict[str, str | bool]] = {}
        for language in ("python", "typescript", "javascript", "go"):
            analyzer = self.analyzer_for(language)
            result[language] = {
                "available": not isinstance(analyzer, LexicalFallbackAnalyzer),
                "capability_tier": analyzer.capability_tier.value,
                "analyzer": type(analyzer).__name__,
            }
            detail = getattr(analyzer, "_errors", {}).get(language)
            if detail:
                result[language]["detail"] = detail
        return result

    def analyze_file(
        self, *, path: Path, relative: str, source: str, language: str,
    ) -> AnalysisResult:
        analyzer = self.analyzer_for(language)
        try:
            return analyzer.analyze_file(
                path=path, relative=relative, source=source, language=language,
            )
        except (SyntaxError, ValueError, RuntimeError) as exc:
            if isinstance(analyzer, LexicalFallbackAnalyzer):
                raise
            fallback = LexicalFallbackAnalyzer().analyze_file(
                path=path, relative=relative, source=source, language=language,
            )
            return AnalysisResult(
                fallback.language, fallback.capability_tier, fallback.confidence,
                fallback.source, fallback.symbols, fallback.imports,
                fallback.references, fallback.calls,
                f"{type(exc).__name__}: {exc}",
            )


__all__ = [
    "AnalysisResult", "AnalyzerRegistry", "CapabilityTier", "ImportRecord",
    "LanguageAnalyzer", "LexicalFallbackAnalyzer", "PythonAstAnalyzer",
    "RawCallRecord", "ReferenceRecord", "SymbolRecord", "TreeSitterAnalyzer",
    "discover_module_boundaries", "module_id_for_path", "module_name_for_path",
    "symbol_id_for",
]
