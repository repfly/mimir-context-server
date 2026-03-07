"""Parser port — interface for extracting symbols from source files."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


@dataclass
class Symbol:
    """A code symbol extracted by a parser.

    This is the common intermediate representation shared between the
    LSP parser and the tree-sitter fallback parser.
    """

    name: str
    kind: str  # mapped to NodeKind by the graph builder
    relative_path: str
    start_line: int
    end_line: int
    code: str
    signature: Optional[str] = None
    docstring: Optional[str] = None

    # Dependencies detected during parsing
    calls: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    type_refs: list[str] = field(default_factory=list)
    decorators: list[str] = field(default_factory=list)


@runtime_checkable
class Parser(Protocol):
    """Interface for source code parsers.

    Implementations: ``TreeSitterParser``, ``LspParser``.
    """

    async def parse_file(
        self,
        file_path: str,
        language: Optional[str] = None,
    ) -> list[Symbol]:
        """Extract all symbols from a single source file.

        Parameters
        ----------
        file_path
            Absolute path to the source file.
        language
            Optional language hint (e.g. ``"python"``, ``"typescript"``).
            If not provided, the parser should infer from file extension.

        Returns
        -------
        list[Symbol]
            All symbols found in the file (functions, classes, methods, etc.).

        Raises
        ------
        ParsingError
            If the file cannot be parsed.
        """
        ...

    def supported_extensions(self) -> frozenset[str]:
        """File extensions this parser can handle (e.g. ``{".py", ".pyi"}``)."""
        ...
