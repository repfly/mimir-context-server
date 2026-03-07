"""Tree-sitter based source code parser.

Fallback (and often primary) parser that uses tree-sitter grammars to
extract symbols from source files.  Supports Python, TypeScript/JavaScript,
Go, Java, Rust, C/C++.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from mimir.domain.errors import ParsingError
from mimir.ports.parser import Symbol

logger = logging.getLogger(__name__)

# Mapping from file extension to tree-sitter language name
_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".swift": "swift",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "c_sharp",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
}

# Node types that represent symbols we want to extract per language
_SYMBOL_QUERIES: dict[str, dict[str, str]] = {
    "python": {
        "function": "function_definition",
        "class": "class_definition",
        "method": "function_definition",  # inside a class
    },
    "javascript": {
        "function": "function_declaration",
        "class": "class_declaration",
        "method": "method_definition",
    },
    "typescript": {
        "function": "function_declaration",
        "class": "class_declaration",
        "method": "method_definition",
    },
    "go": {
        "function": "function_declaration",
        "method": "method_declaration",
        "type": "type_declaration",
    },
    "java": {
        "function": "method_declaration",
        "class": "class_declaration",
    },
    "rust": {
        "function": "function_item",
        "class": "struct_item",
        "method": "function_item",
    },
}


class TreeSitterParser:
    """Parser implementation backed by tree-sitter.

    Lazily initialises language grammars on first use to avoid import-time
    side effects.
    """

    def __init__(self) -> None:
        self._parsers: dict[str, object] = {}
        self._initialized = False

    def _ensure_init(self) -> None:
        if self._initialized:
            return
        try:
            import tree_sitter_languages  # noqa: F401
            self._initialized = True
        except ImportError:
            logger.warning(
                "tree-sitter-languages not installed. "
                "Install with: pip install tree-sitter-languages"
            )
            self._initialized = True  # prevent repeated warnings

    def _get_parser(self, language: str):
        """Get or create a tree-sitter parser for the given language."""
        if language in self._parsers:
            return self._parsers[language]
        try:
            from tree_sitter_languages import get_parser
            parser = get_parser(language)
            self._parsers[language] = parser
            return parser
        except Exception as exc:
            logger.debug("No tree-sitter grammar for %s: %s", language, exc)
            return None

    def supported_extensions(self) -> frozenset[str]:
        return frozenset(_EXT_TO_LANG.keys())

    async def parse_file(
        self,
        file_path: str,
        language: Optional[str] = None,
    ) -> list[Symbol]:
        """Parse a source file and extract symbols."""
        self._ensure_init()

        ext = os.path.splitext(file_path)[1].lower()
        lang = language or _EXT_TO_LANG.get(ext)
        if not lang:
            return []

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except OSError as exc:
            raise ParsingError(file_path, str(exc)) from exc

        parser = self._get_parser(lang)
        if parser is None:
            # Fallback: extract minimal info without grammar
            return self._extract_minimal(file_path, source, lang)

        try:
            tree = parser.parse(source.encode("utf-8"))
            return self._extract_symbols(tree.root_node, source, file_path, lang)
        except Exception as exc:
            raise ParsingError(file_path, f"tree-sitter parse failed: {exc}") from exc

    def _extract_symbols(
        self,
        root_node,
        source: str,
        file_path: str,
        language: str,
    ) -> list[Symbol]:
        """Walk the AST and extract symbol definitions."""
        symbols: list[Symbol] = []
        lines = source.split("\n")
        relative_path = file_path  # caller converts to relative

        queries = _SYMBOL_QUERIES.get(language, {})
        func_types = {queries.get("function"), queries.get("method")} - {None}
        class_types = {queries.get("class")} - {None}
        type_types = {queries.get("type")} - {None}

        def walk(node, parent_class: Optional[str] = None):
            node_type = node.type

            if node_type in func_types:
                sym = self._node_to_symbol(
                    node, lines, relative_path, language,
                    kind="method" if parent_class else "function",
                )
                if sym:
                    symbols.append(sym)

            elif node_type in class_types:
                sym = self._node_to_symbol(
                    node, lines, relative_path, language, kind="class",
                )
                if sym:
                    symbols.append(sym)
                # Recurse into class body to find methods
                for child in node.children:
                    walk(child, parent_class=sym.name if sym else None)
                return  # don't double-recurse

            elif node_type in type_types:
                sym = self._node_to_symbol(
                    node, lines, relative_path, language, kind="type",
                )
                if sym:
                    symbols.append(sym)

            for child in node.children:
                walk(child, parent_class)

        walk(root_node)
        return symbols

    def _node_to_symbol(
        self,
        node,
        lines: list[str],
        relative_path: str,
        language: str,
        kind: str,
    ) -> Optional[Symbol]:
        """Convert a tree-sitter node to a Symbol."""
        # Find the name child
        name = None
        for child in node.children:
            if child.type in ("identifier", "name", "type_identifier", "property_identifier"):
                name = child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
                break

        if not name:
            return None

        start_line = node.start_point[0] + 1  # 1-indexed
        end_line = node.end_point[0] + 1
        code = "\n".join(lines[start_line - 1 : end_line])

        # Extract signature (first line of the definition)
        signature = lines[start_line - 1].strip() if start_line <= len(lines) else None

        # Extract docstring (language-specific)
        docstring = self._extract_docstring(node, lines, language)

        # Extract decorators
        decorators = self._extract_decorators(node, lines)

        return Symbol(
            name=name,
            kind=kind,
            relative_path=relative_path,
            start_line=start_line,
            end_line=end_line,
            code=code,
            signature=signature,
            docstring=docstring,
            decorators=decorators,
        )

    def _extract_docstring(self, node, lines: list[str], language: str) -> Optional[str]:
        """Extract docstring from AST node."""
        if language == "python":
            # Python docstrings are expression_statement > string children
            for child in node.children:
                if child.type == "block":
                    for block_child in child.children:
                        if block_child.type == "expression_statement":
                            for expr_child in block_child.children:
                                if expr_child.type == "string":
                                    text = expr_child.text
                                    if isinstance(text, bytes):
                                        text = text.decode("utf-8")
                                    # Strip quote marks
                                    return text.strip("\"'").strip()
        return None

    def _extract_decorators(self, node, lines: list[str]) -> list[str]:
        """Extract decorator names from AST node."""
        decorators: list[str] = []
        for child in node.children:
            if child.type == "decorator":
                text = child.text
                if isinstance(text, bytes):
                    text = text.decode("utf-8")
                decorators.append(text.strip())
        return decorators

    def _extract_minimal(
        self,
        file_path: str,
        source: str,
        language: str,
    ) -> list[Symbol]:
        """Regex-based minimal extraction when no grammar is available."""
        import re
        symbols: list[Symbol] = []
        lines = source.split("\n")

        # Python-style function/class detection
        patterns = {
            "python": [
                (r"^\s*def\s+(\w+)", "function"),
                (r"^\s*class\s+(\w+)", "class"),
                (r"^\s*async\s+def\s+(\w+)", "function"),
            ],
            "javascript": [
                (r"^\s*function\s+(\w+)", "function"),
                (r"^\s*class\s+(\w+)", "class"),
                (r"^\s*(?:export\s+)?const\s+(\w+)\s*=.*=>", "function"),
            ],
        }

        for pattern, kind in patterns.get(language, []):
            for i, line in enumerate(lines):
                match = re.match(pattern, line)
                if match:
                    name = match.group(1)
                    # Find end of block (rough: next non-indented line)
                    end = i + 1
                    if i + 1 < len(lines):
                        indent = len(line) - len(line.lstrip())
                        for j in range(i + 1, min(i + 200, len(lines))):
                            if lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) <= indent:
                                end = j
                                break
                        else:
                            end = min(i + 200, len(lines))

                    symbols.append(Symbol(
                        name=name,
                        kind=kind,
                        relative_path=file_path,
                        start_line=i + 1,
                        end_line=end,
                        code="\n".join(lines[i:end]),
                        signature=line.strip(),
                    ))

        return symbols
