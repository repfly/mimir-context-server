"""Workspace registry — maps workspace names to config file paths.

The registry lives at ``~/.treedex/workspaces.toml`` and is managed by the
``mimir workspace`` CLI sub-commands.  It is read-only at runtime; the MCP
server or any other command only resolves a name → path, never writes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from mimir.domain.errors import ConfigError

logger = logging.getLogger(__name__)

_DEFAULT_REGISTRY_PATH = Path.home() / ".mimir" / "workspaces.toml"


class WorkspaceRegistry:
    """Persistent registry of named Mimir workspaces."""

    def __init__(self, registry_path: Optional[Path] = None) -> None:
        self._path = registry_path or _DEFAULT_REGISTRY_PATH

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def _load_raw(self) -> dict:
        """Load and return the raw TOML dict, or an empty dict if not found."""
        if not self._path.exists():
            return {}
        import tomli
        with open(self._path, "rb") as f:
            return tomli.load(f)

    def list(self) -> dict[str, Path]:
        """Return all registered workspaces as {name: config_path}."""
        raw = self._load_raw()
        return {
            name: Path(path_str)
            for name, path_str in raw.get("workspaces", {}).items()
        }

    def resolve(self, name: str) -> Path:
        """Return the config path for a workspace name.

        Raises ``ConfigError`` if the name is not registered.
        """
        workspaces = self.list()
        if name not in workspaces:
            known = ", ".join(sorted(workspaces)) or "(none registered)"
            raise ConfigError(
                f"Workspace '{name}' not found in registry. "
                f"Known workspaces: {known}. "
                f"Add it with: mimir workspace add {name} --config <path>"
            )
        config_path = workspaces[name]
        if not config_path.is_file():
            raise ConfigError(
                f"Workspace '{name}' points to a config that no longer exists: {config_path}"
            )
        return config_path

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def _save_raw(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Build TOML manually to avoid adding tomli-w as a dep
        lines = ["[workspaces]\n"]
        for name, path in sorted(data.get("workspaces", {}).items()):
            escaped = str(path).replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{name} = "{escaped}"\n')
        self._path.write_text("".join(lines))

    def add(self, name: str, config_path: Path) -> None:
        """Register a workspace. Overwrites any existing entry with the same name."""
        _validate_name(name)
        config_path = config_path.resolve()
        if not config_path.is_file():
            raise ConfigError(f"Config file not found: {config_path}")

        raw = self._load_raw()
        raw.setdefault("workspaces", {})[name] = str(config_path)
        self._save_raw(raw)
        logger.info("Registered workspace '%s' → %s", name, config_path)

    def remove(self, name: str) -> None:
        """Unregister a workspace. Raises ``ConfigError`` if not found."""
        raw = self._load_raw()
        workspaces = raw.get("workspaces", {})
        if name not in workspaces:
            raise ConfigError(f"Workspace '{name}' is not registered.")
        del workspaces[name]
        raw["workspaces"] = workspaces
        self._save_raw(raw)
        logger.info("Removed workspace '%s'", name)


def _validate_name(name: str) -> None:
    """Names must be simple identifiers (alphanumeric, hyphens, underscores)."""
    import re
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", name):
        raise ConfigError(
            f"Invalid workspace name '{name}'. "
            "Names may only contain letters, digits, hyphens, and underscores."
        )
