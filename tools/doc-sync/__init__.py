"""Module 01: Doc-Sync Engine (`/tools/doc-sync/__init__.py`).

Self-registers the `doc-sync` subcommand into the Centralized Internal Tooling Suite CLI.
"""

from __future__ import annotations

import argparse

from .cli import register_doc_sync_subcommands

PLUGIN_DESCRIPTION = "Module 01: Synchronize Markdown documentation to Confluence Storage Format"


def register(subparsers: argparse._SubParsersAction) -> None:
    """Plugin registration hook invoked by `tools/common/plugin.py`."""
    register_doc_sync_subcommands(subparsers)
