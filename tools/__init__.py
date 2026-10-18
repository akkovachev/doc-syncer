"""Centralized Internal Tooling Suite root package.

Automatically aliases hyphenated tool directories (e.g. `tools/doc-sync`,
`tools/xray-scan`) as importable Python subpackages (`tools.doc_sync`,
`tools.xray_scan`) so both filesystem paths from the architecture spec and
standard Python module imports work seamlessly.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

__version__ = "1.0.0"

_TOOLS_DIR = Path(__file__).resolve().parent


def _register_hyphenated_subpackages() -> None:
    """Register `tools/<name-with-hyphen>` directories as `tools.<name_with_underscore>`."""
    for child in sorted(_TOOLS_DIR.iterdir()):
        if not child.is_dir() or "-" not in child.name:
            continue
        init_file = child / "__init__.py"
        if not init_file.exists():
            continue
        underscored = child.name.replace("-", "_")
        fq_name = f"{__name__}.{underscored}"
        if fq_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(
            fq_name,
            init_file,
            submodule_search_locations=[str(child)],
        )
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[fq_name] = module
            setattr(sys.modules[__name__], underscored, module)
            spec.loader.exec_module(module)


_register_hyphenated_subpackages()
