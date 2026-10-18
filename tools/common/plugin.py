"""Plugin discovery and CLI subcommand registration contract (`/tools/common/plugin.py`).

Implements Section 2 of the Architecture Proposal:
- `bin/tools` is a thin dispatcher; each tool package under `/tools` (e.g. `tools/doc-sync`,
  `tools/xray-scan`) self-registers via a `register(subparsers)` function in its `__init__.py`.
- Adding a new tool module under `/tools/<new-tool>` requires zero changes to existing modules.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import tools  # triggers hyphenated subpackage aliasing

TOOLS_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RegisteredPlugin:
    """Metadata for a discovered tool plugin under `/tools`."""

    name: str
    package_path: Path
    description: str
    register_fn: Callable[[argparse._SubParsersAction], None]


_PLUGIN_REGISTRY: Dict[str, RegisteredPlugin] = {}


def register_plugin(
    name: str,
    description: str,
    register_fn: Callable[[argparse._SubParsersAction], None],
    package_path: Optional[Path] = None,
) -> None:
    """Explicitly register a tool plugin into the central CLI registry."""
    _PLUGIN_REGISTRY[name] = RegisteredPlugin(
        name=name,
        package_path=package_path or TOOLS_ROOT / name,
        description=description,
        register_fn=register_fn,
    )


def discover_plugins(tools_dir: Path = TOOLS_ROOT) -> Dict[str, RegisteredPlugin]:
    """Scan `/tools/*` (excluding `common`) and invoke each module's `register` hook."""
    for child in sorted(tools_dir.iterdir()):
        if not child.is_dir() or child.name.startswith(("_", ".")) or child.name == "common":
            continue
        init_file = child / "__init__.py"
        if not init_file.exists():
            continue

        underscored = child.name.replace("-", "_")
        fq_name = f"tools.{underscored}"
        module = sys.modules.get(fq_name)
        if module is None:
            spec = importlib.util.spec_from_file_location(
                fq_name,
                init_file,
                submodule_search_locations=[str(child)],
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[fq_name] = module
                spec.loader.exec_module(module)

        if module is not None and child.name not in _PLUGIN_REGISTRY:
            reg_fn = getattr(module, "register", None)
            if callable(reg_fn):
                desc = getattr(module, "PLUGIN_DESCRIPTION", f"{child.name} module")
                register_plugin(child.name, desc, reg_fn, package_path=child)

    return dict(_PLUGIN_REGISTRY)


def build_root_parser() -> argparse.ArgumentParser:
    """Construct the root `tools` CLI ArgumentParser with all discovered plugins attached."""
    parser = argparse.ArgumentParser(
        prog="tools",
        description="Centralized Internal Tooling Suite CLI (`tools <module> <subcommand>`)",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"internal-tools {tools.__version__}",
    )
    subparsers = parser.add_subparsers(
        dest="tool_module",
        title="available tool modules",
        metavar="<module>",
    )

    plugins = discover_plugins()
    for plugin in plugins.values():
        plugin.register_fn(subparsers)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main CLI dispatcher entrypoint (`tools <module> <subcommand>`)."""
    parser = build_root_parser()
    args = parser.parse_args(argv)

    handler = getattr(args, "handler", None)
    if callable(handler):
        return int(handler(args))

    parser.print_help()
    return 1


def doc_sync_Entrypoint(argv: Optional[Sequence[str]] = None) -> int:
    """Direct `doc-sync <subcommand>` convenience entrypoint (forwards to `tools doc-sync ...`)."""
    args_list: List[str] = list(argv) if argv is not None else sys.argv[1:]
    return main(["doc-sync", *args_list])
