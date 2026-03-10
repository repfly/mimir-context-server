"""Tree-sitter based source code parser.

Language-agnostic symbol extraction: any tree-sitter node whose type
contains "declaration", "definition", or "item" and that has an
identifier child is treated as a symbol.  No per-language config needed.
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
    ".lua": "lua",
    ".php": "php",
    ".scala": "scala",
    ".zig": "zig",
    ".ex": "elixir",
    ".exs": "elixir",
    ".hs": "haskell",
    ".ml": "ocaml",
    ".mli": "ocaml",
    ".r": "r",
    ".R": "r",
    ".dart": "dart",
    ".v": "v",
    ".jl": "julia",
}

# Suffixes that indicate a node is a symbol declaration/definition
_DECL_SUFFIXES = ("_declaration", "_definition", "_item")

# Node types to always skip (noise, not real symbols)
_SKIP_TYPES = frozenset({
    "expression_statement", "assignment", "variable_declaration",
    "lexical_declaration", "short_var_declaration", "const_declaration",
    "let_declaration", "var_declaration", "field_declaration",
    "parameter", "parameter_declaration", "argument_list",
    "import_declaration", "import_statement", "include_statement",
    "package_declaration", "comment", "line_comment", "block_comment",
    "ERROR",
})

# Node type substrings that indicate a function-like symbol
_FUNC_HINTS = ("function", "method", "func", "constructor", "initializer")
# Node type substrings that indicate a class/type-like symbol
_CLASS_HINTS = ("class", "struct", "enum", "protocol", "interface",
                "trait", "impl", "type", "module", "namespace", "object")

# Child node types that carry the symbol name
_NAME_TYPES = frozenset({
    "identifier", "name", "type_identifier", "property_identifier",
    "simple_identifier", "field_identifier",
})


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
            self._get_parser_fn = self._resolve_parser_fn()
            self._initialized = True
        except ImportError:
            logger.warning(
                "No tree-sitter language pack installed. "
                "Install with: pip install tree-sitter-language-pack"
            )
            self._get_parser_fn = None
            self._initialized = True  # prevent repeated warnings

    @staticmethod
    def _resolve_parser_fn():
        """Return get_parser from whichever package is available."""
        try:
            from tree_sitter_language_pack import get_parser
            return get_parser
        except ImportError:
            from tree_sitter_languages import get_parser
            return get_parser

    def _get_parser(self, language: str):
        """Get or create a tree-sitter parser for the given language."""
        if language in self._parsers:
            return self._parsers[language]
        if not self._get_parser_fn:
            return None
        try:
            parser = self._get_parser_fn(language)
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
            return self._extract_minimal(file_path, source)

        try:
            tree = parser.parse(source.encode("utf-8"))
            return self._extract_symbols(tree.root_node, source, file_path, lang)
        except Exception as exc:
            raise ParsingError(file_path, f"tree-sitter parse failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Generic AST extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_node(node_type: str) -> Optional[str]:
        """Return 'function', 'class', or None based on the node type string."""
        if node_type in _SKIP_TYPES:
            return None

        # Must look like a declaration / definition / item
        if not any(node_type.endswith(s) for s in _DECL_SUFFIXES):
            return None

        lowered = node_type.lower()
        if any(h in lowered for h in _FUNC_HINTS):
            return "function"
        if any(h in lowered for h in _CLASS_HINTS):
            return "class"

        # Catch-all: still a declaration, treat as function
        return "function"

    def _extract_symbols(
        self,
        root_node,
        source: str,
        file_path: str,
        language: str,
    ) -> list[Symbol]:
        """Walk the AST and extract symbol definitions (language-agnostic)."""
        symbols: list[Symbol] = []
        lines = source.split("\n")

        def walk(node, inside_class: bool = False):
            kind = self._classify_node(node.type)

            if kind == "function":
                sym = self._node_to_symbol(
                    node, lines, file_path, language,
                    kind="method" if inside_class else "function",
                )
                if sym:
                    symbols.append(sym)

            elif kind == "class":
                sym = self._node_to_symbol(
                    node, lines, file_path, language, kind="class",
                )
                if sym:
                    symbols.append(sym)
                # Recurse into body to find methods
                for child in node.children:
                    walk(child, inside_class=True)
                return  # don't double-recurse

            for child in node.children:
                walk(child, inside_class)

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
        name = None
        for child in node.children:
            if child.type in _NAME_TYPES:
                name = child.text.decode("utf-8") if isinstance(child.text, bytes) else child.text
                break

        if not name:
            return None

        start_line = node.start_point[0] + 1  # 1-indexed
        end_line = node.end_point[0] + 1
        code = "\n".join(lines[start_line - 1 : end_line])

        signature = lines[start_line - 1].strip() if start_line <= len(lines) else None

        docstring = self._extract_docstring(node, lines, language)
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

    # ------------------------------------------------------------------
    # Docstring / decorator helpers
    # ------------------------------------------------------------------

    def _extract_docstring(self, node, lines: list[str], language: str) -> Optional[str]:
        """Extract docstring from AST node."""
        # Python: expression_statement > string inside block
        if language == "python":
            for child in node.children:
                if child.type == "block":
                    for block_child in child.children:
                        if block_child.type == "expression_statement":
                            for expr_child in block_child.children:
                                if expr_child.type == "string":
                                    text = expr_child.text
                                    if isinstance(text, bytes):
                                        text = text.decode("utf-8")
                                    return text.strip("\"'").strip()

        # Generic: look for a comment node immediately before or as first child
        for child in node.children:
            if child.type in ("comment", "line_comment", "block_comment"):
                text = child.text
                if isinstance(text, bytes):
                    text = text.decode("utf-8")
                return text.lstrip("/#* ").rstrip("* /").strip()
            # Stop at the first non-comment child
            if child.type not in ("decorator", "attribute"):
                break

        return None

    def _extract_decorators(self, node, lines: list[str]) -> list[str]:
        """Extract decorator / attribute names from AST node."""
        decorators: list[str] = []
        for child in node.children:
            if child.type in ("decorator", "attribute"):
                text = child.text
                if isinstance(text, bytes):
                    text = text.decode("utf-8")
                decorators.append(text.strip())
        return decorators

    # ------------------------------------------------------------------
    # Regex fallback (no grammar available)
    # ------------------------------------------------------------------

    def _extract_minimal(
        self,
        file_path: str,
        source: str,
    ) -> list[Symbol]:
        """Regex-based minimal extraction when no grammar is available.

        Uses generic patterns that work across most C-family and
        scripting languages.
        """
        import re
        symbols: list[Symbol] = []
        lines = source.split("\n")

        # Generic patterns that cover most languages
        generic_patterns = [
            # function/method: func/def/fn/fun keyword
            (r"^\s*(?:(?:pub(?:lic)?|priv(?:ate)?|prot(?:ected)?|internal|open|fileprivate|static|async|override|final|abstract|virtual)\s+)*"
             r"(?:func|def|fn|fun|function)\s+(\w+)", "function"),
            # class-like: class/struct/enum/protocol/interface/trait
            (r"^\s*(?:(?:pub(?:lic)?|priv(?:ate)?|prot(?:ected)?|internal|open|fileprivate|static|final|abstract|sealed)\s+)*"
             r"(?:class|struct|enum|protocol|interface|trait|object)\s+(\w+)", "class"),
        ]

        for pattern, kind in generic_patterns:
            for i, line in enumerate(lines):
                match = re.match(pattern, line)
                if match:
                    name = match.group(1)
                    # Find end of block (rough heuristic)
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
