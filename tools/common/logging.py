"""Structured logging and GitHub Actions workflow annotations (`/tools/common/logging.py`).

Supports:
- Structured console logging
- GitHub Actions workflow commands (`::notice`, `::warning`, `::error`, `::group`, `::add-mask`)
- Appending rich Markdown summaries to `$GITHUB_STEP_SUMMARY`
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional


def _escape_actions_property(val: str) -> str:
    return (
        val.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def _escape_actions_data(val: str) -> str:
    return val.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


@dataclass
class ActionsLogger:
    """Logger that emits both structured human-readable logs and GitHub Actions annotations."""

    name: str
    verbose: bool = False

    @property
    def is_github_actions(self) -> bool:
        return os.environ.get("GITHUB_ACTIONS", "").lower() == "true"

    def _emit(self, level: str, message: str, stream=None) -> None:
        out = stream or sys.stdout
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[{ts}] [{self.name}] [{level}] {message}", file=out, flush=True)

    def debug(self, message: str) -> None:
        if self.verbose or os.environ.get("RUNNER_DEBUG") == "1":
            if self.is_github_actions:
                print(f"::debug::{_escape_actions_data(message)}", flush=True)
            else:
                self._emit("DEBUG", message)

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def annotate(
        self,
        level: str,
        message: str,
        *,
        file: Optional[str] = None,
        line: Optional[int] = None,
        title: Optional[str] = None,
    ) -> None:
        """Emit a GitHub Actions workflow annotation (`notice`, `warning`, or `error`)."""
        props = []
        if file:
            props.append(f"file={_escape_actions_property(str(file))}")
        if line is not None:
            props.append(f"line={line}")
        if title:
            props.append(f"title={_escape_actions_property(title)}")

        prop_str = f" {','.join(props)}" if props else ""
        if self.is_github_actions:
            stream = sys.stderr if level == "error" else sys.stdout
            print(f"::{level}{prop_str}::{_escape_actions_data(message)}", file=stream, flush=True)
        else:
            prefix = f"{title}: " if title else ""
            loc = f" ({file}" + (f":{line})" if line else ")") if file else ""
            stream = sys.stderr if level == "error" else sys.stdout
            self._emit(level.upper(), f"{prefix}{message}{loc}", stream=stream)

    def notice(
        self,
        message: str,
        *,
        file: Optional[str] = None,
        line: Optional[int] = None,
        title: Optional[str] = None,
    ) -> None:
        self.annotate("notice", message, file=file, line=line, title=title)

    def warning(
        self,
        message: str,
        *,
        file: Optional[str] = None,
        line: Optional[int] = None,
        title: Optional[str] = None,
    ) -> None:
        self.annotate("warning", message, file=file, line=line, title=title)

    def error(
        self,
        message: str,
        *,
        file: Optional[str] = None,
        line: Optional[int] = None,
        title: Optional[str] = None,
    ) -> None:
        self.annotate("error", message, file=file, line=line, title=title)

    def mask_secret(self, secret_value: str) -> None:
        """Mask a secret value in GitHub Actions logs so it can never be printed."""
        if secret_value and self.is_github_actions:
            for line in secret_value.splitlines():
                if line.strip():
                    print(f"::add-mask::{_escape_actions_data(line.strip())}", flush=True)

    @contextmanager
    def group(self, title: str) -> Iterator[None]:
        if self.is_github_actions:
            print(f"::group::{_escape_actions_data(title)}", flush=True)
        else:
            self.info(f"=== {title} ===")
        try:
            yield
        finally:
            if self.is_github_actions:
                print("::endgroup::", flush=True)

    def write_job_summary(self, markdown_content: str) -> Optional[Path]:
        """Append Markdown content to `$GITHUB_STEP_SUMMARY` if set."""
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if not summary_path:
            return None
        path = Path(summary_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(markdown_content)
            if not markdown_content.endswith("\n"):
                fh.write("\n")
        return path


def get_logger(name: str = "tools", verbose: bool = False) -> ActionsLogger:
    return ActionsLogger(name=name, verbose=verbose)
