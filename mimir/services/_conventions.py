"""Shared naming conventions for test file discovery."""

from __future__ import annotations

import os
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from mimir.domain.graph import CodeGraph
    from mimir.domain.models import Node


def find_test_file(file_path: str, graph: "CodeGraph") -> Optional["Node"]:
    """Find the test file associated with a source file by naming convention.

    Supports common conventions across languages:
    - Python:  foo.py  → test_foo.py, foo_test.py
    - Swift:   Foo.swift → FooTests.swift
    - TS/JS:   Foo.ts → Foo.test.ts, Foo.spec.ts
    - Kotlin:  Foo.kt → FooTest.kt
    - Java:    Foo.java → FooTest.java
    """
    if not file_path:
        return None

    base = os.path.basename(file_path)
    name, ext = os.path.splitext(base)

    # Build candidate test file basenames
    candidates: list[str] = []

    if ext == ".py":
        candidates.append(f"test_{name}.py")
        candidates.append(f"{name}_test.py")
    elif ext == ".swift":
        candidates.append(f"{name}Tests.swift")
        candidates.append(f"{name}Test.swift")
    elif ext in (".ts", ".tsx"):
        candidates.append(f"{name}.test{ext}")
        candidates.append(f"{name}.spec{ext}")
    elif ext in (".js", ".jsx"):
        candidates.append(f"{name}.test{ext}")
        candidates.append(f"{name}.spec{ext}")
    elif ext == ".kt":
        candidates.append(f"{name}Test.kt")
    elif ext == ".java":
        candidates.append(f"{name}Test.java")
    elif ext == ".go":
        candidates.append(f"{name}_test.go")
    elif ext == ".rs":
        # Rust uses mod tests inside the file, not separate files typically
        pass
    else:
        # Generic fallback
        candidates.append(f"test_{name}{ext}")
        candidates.append(f"{name}_test{ext}")

    if not candidates:
        return None

    # Search graph for a file node matching any candidate
    candidate_set = set(candidates)
    for node in graph.all_nodes():
        if node.path and os.path.basename(node.path) in candidate_set:
            return node

    return None
