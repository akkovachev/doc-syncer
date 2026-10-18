"""Module 02 (Roadmap Stub): JFrog Xray Security Scan (`/tools/xray-scan/__init__.py`).

Demonstrates the zero-refactor plugin contract defined in `tools/common/plugin.py`:
adding a new package under `/tools` automatically registers the `xray-scan` subcommand
on the unified `tools` CLI without modifying `doc-sync` or `tools/common`.
"""

from __future__ import annotations

import argparse

from tools.common.logging import get_logger

PLUGIN_DESCRIPTION = "Module 02 (Roadmap): JFrog CLI + Xray security scan & SARIF gate"


def _handle_xray_stub(args: argparse.Namespace) -> int:
    log = get_logger("xray-scan")
    log.info(
        f"JFrog Xray Scan module stub invoked for target='{args.target}' "
        f"(fail-severity={args.fail_severity}). Scheduled for Phase 4 rollout."
    )
    return 0


def register(subparsers: argparse._SubParsersAction) -> None:
    """Register `tools xray-scan` subcommand."""
    xray_parser = subparsers.add_parser(
        "xray-scan",
        help="Module 02 (Roadmap): Run JFrog Xray security scan and emit SARIF report",
        description="Module 02: JFrog CLI wrapper, SARIF formatting, and vulnerability gate.",
    )
    xray_parser.add_argument("--target", default=".", help="Artifact or directory path to scan")
    xray_parser.add_argument(
        "--fail-severity",
        default="high",
        choices=["low", "medium", "high", "critical"],
        help="Minimum vulnerability severity that fails the gate",
    )
    xray_parser.set_defaults(handler=_handle_xray_stub)
