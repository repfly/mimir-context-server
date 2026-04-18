"""Backward-compatible CLI entrypoint."""

from mimir.adapters.cli_support import app

__all__ = ["app"]


if __name__ == "__main__":
    app()
