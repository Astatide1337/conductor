"""Thin entrypoint shim — delegates to conductor.cli:app."""

from conductor.cli import app

if __name__ == "__main__":
    app()