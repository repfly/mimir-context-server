"""Language detection from file paths."""

from __future__ import annotations

import os

# Extension → language name (for syntax highlighting and prompts)
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


def detect_language(path: str | None) -> str:
    """Return the language name for a file path, or empty string if unknown."""
    if not path:
        return ""
    ext = os.path.splitext(path)[1].lower()
    return _EXT_TO_LANG.get(ext, "")
