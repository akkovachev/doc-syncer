"""Discovery Engine (`/tools/doc-sync/discovery.py`).

Implements Section 3.2 & Section 4 of the Architecture Proposal:
- Validates `.doc-sync.yml` against `schema/doc_sync_config.schema.json` before any network I/O.
- Walks the repository tree, expands exact paths and glob patterns (`docs/**/*.md`),
  and verifies matched Markdown files exist and are non-empty.
- Emits warning annotations (or fails if `on_missing_file: fail`) when files/globs match nothing.
- Derives page titles (explicit `title`, first `# H1` heading when `title_from: h1`, or filename)
  and builds a cross-document link resolution table (`repo_rel_path -> LinkTarget`).
"""

from __future__ import annotations

import glob
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from tools.common.config import ConfigValidationError, load_and_validate_config
from tools.common.logging import ActionsLogger, get_logger

SCHEMA_PATH = Path(__file__).resolve().parent / "schema" / "doc_sync_config.schema.json"
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*#*$", re.MULTILINE)


class DiscoveryError(RuntimeError):
    """Raised when discovery encounters a fatal file error (`on_missing_file: fail`)."""


@dataclass
class DocSyncDefaults:
    """Resolved defaults section from `.doc-sync.yml`."""

    force_overwrite: bool = False
    add_source_banner: bool = True
    on_missing_file: str = "warn"  # "warn" | "fail"
    agent_review: bool = False


@dataclass
class SyncTarget:
    """A single resolved Markdown file mapped to a Confluence destination."""

    source_rel_path: str  # POSIX-normalized repo-relative path (e.g. "docs/guide.md")
    abs_path: Path
    markdown_content: str
    space_key: str
    page_id: Optional[str] = None
    parent_page_id: Optional[str] = None
    title: str = ""
    labels: List[str] = field(default_factory=list)
    force_overwrite: bool = False
    add_source_banner: bool = True


@dataclass
class DiscoveryResult:
    """Output of the Discovery Engine."""

    config_path: Path
    repo_root: Path
    raw_config: Dict[str, Any]
    space_key: str
    base_url_secret: str
    auth_secret: str
    username_secret: Optional[str]
    slack_webhook_secret: Optional[str]
    defaults: DocSyncDefaults
    targets: List[SyncTarget]
    link_map: Dict[str, SyncTarget]  # repo-relative POSIX path -> SyncTarget
    warnings: List[str] = field(default_factory=list)


def extract_first_h1(markdown_text: str) -> Optional[str]:
    """Extract the text of the first ATX `# Heading` outside of fenced code blocks."""
    # Strip fenced code blocks first so `# comment` inside a bash block isn't mistaken for an H1
    stripped = re.sub(r"```[\s\S]*?```", "", markdown_text)
    stripped = re.sub(r"~~~[\s\S]*?~~~", "", stripped)
    match = _H1_RE.search(stripped)
    if match:
        heading = match.group(1).strip()
        # Strip inline markdown formatting like `code` or **bold**
        heading = re.sub(r"[`*_~]", "", heading).strip()
        if heading:
            return heading
    return None


def humanize_filename(rel_path: str) -> str:
    """Convert a relative Markdown path like `docs/getting-started.md` into a readable title."""
    stem = Path(rel_path).stem
    if stem.upper() in ("README", "INDEX") and Path(rel_path).parent.name:
        stem = f"{Path(rel_path).parent.name} — {stem}"
    cleaned = re.sub(r"[-_]+", " ", stem).strip()
    return cleaned.title() if cleaned.islower() else cleaned


def derive_title(
    rel_path: str,
    markdown_text: str,
    explicit_title: Optional[str] = None,
    title_from: Optional[str] = None,
) -> str:
    """Resolve the Confluence page title for a synced Markdown file."""
    if explicit_title:
        return explicit_title.strip()
    if title_from == "h1":
        h1 = extract_first_h1(markdown_text)
        if h1:
            return h1
    elif title_from == "filename":
        return humanize_filename(rel_path)

    # Default fallback: try H1 first, then humanized filename
    h1 = extract_first_h1(markdown_text)
    return h1 if h1 else humanize_filename(rel_path)


def _is_glob_pattern(pattern: str) -> bool:
    return any(ch in pattern for ch in ("*", "?", "["))


def discover_repository(
    repo_root: Union[str, Path],
    config_file: Union[str, Path] = ".doc-sync.yml",
    *,
    schema_path: Path = SCHEMA_PATH,
    logger: Optional[ActionsLogger] = None,
) -> DiscoveryResult:
    """Validate `.doc-sync.yml`, walk the repo, and build the list of `SyncTarget`s."""
    log = logger or get_logger("discovery")
    root_path = Path(repo_root).resolve()
    cfg_path = Path(config_file)
    if not cfg_path.is_absolute():
        cfg_path = (root_path / cfg_path).resolve()

    raw_cfg = load_and_validate_config(cfg_path, schema_path)

    conf_block = raw_cfg["confluence"]
    space_key = str(conf_block["space_key"])
    base_url_secret = str(conf_block["base_url_secret"])
    auth_secret = str(conf_block["auth_secret"])
    username_secret = conf_block.get("username_secret")

    defaults_block = raw_cfg.get("defaults", {}) or {}
    defaults = DocSyncDefaults(
        force_overwrite=bool(defaults_block.get("force_overwrite", False)),
        add_source_banner=bool(defaults_block.get("add_source_banner", True)),
        on_missing_file=str(defaults_block.get("on_missing_file", "warn")),
        agent_review=bool(defaults_block.get("agent_review", False)),
    )

    notif_block = raw_cfg.get("notifications", {}) or {}
    slack_secret = notif_block.get("slack_webhook_secret")

    targets: List[SyncTarget] = []
    link_map: Dict[str, SyncTarget] = {}
    warnings: List[str] = []

    def _handle_missing(msg: str, src_pattern: str) -> None:
        warnings.append(msg)
        if defaults.on_missing_file == "fail":
            log.error(msg, file=str(cfg_path.relative_to(root_path) if cfg_path.is_relative_to(root_path) else cfg_path), title="Missing Mapped File")
            raise DiscoveryError(msg)
        log.warning(msg, file=src_pattern, title="Missing Mapped File")

    for entry in raw_cfg.get("mappings", []):
        source_pattern = str(entry["source"]).replace("\\", "/")
        page_id = entry.get("confluence_page_id")
        parent_page_id = entry.get("confluence_parent_page_id")
        explicit_title = entry.get("title")
        title_from = entry.get("title_from")
        labels = [str(lbl) for lbl in entry.get("labels", [])]
        force_overwrite = bool(entry.get("force_overwrite", defaults.force_overwrite))
        add_source_banner = bool(entry.get("add_source_banner", defaults.add_source_banner))

        if _is_glob_pattern(source_pattern):
            matched_paths = [
                Path(p).resolve()
                for p in sorted(glob.glob(str(root_path / source_pattern), recursive=True))
                if Path(p).is_file()
            ]
            if not matched_paths:
                _handle_missing(
                    f"Glob pattern '{source_pattern}' matched 0 files in repository.",
                    source_pattern,
                )
                continue
        else:
            candidate = (root_path / source_pattern).resolve()
            if not candidate.exists() or not candidate.is_file():
                _handle_missing(
                    f"Mapped file '{source_pattern}' does not exist in repository.",
                    source_pattern,
                )
                continue
            matched_paths = [candidate]

        for abs_file in matched_paths:
            try:
                rel_posix = abs_file.relative_to(root_path).as_posix()
            except ValueError:
                rel_posix = abs_file.name

            content = abs_file.read_text(encoding="utf-8")
            if not content.strip():
                _handle_missing(
                    f"Mapped Markdown file '{rel_posix}' is empty.",
                    rel_posix,
                )
                continue

            # If a glob matches multiple files and an explicit `title` was somehow set, append stem
            effective_explicit = (
                explicit_title
                if (explicit_title and len(matched_paths) == 1)
                else None
            )
            resolved_title = derive_title(
                rel_posix,
                content,
                explicit_title=effective_explicit,
                title_from=title_from,
            )

            target = SyncTarget(
                source_rel_path=rel_posix,
                abs_path=abs_file,
                markdown_content=content,
                space_key=space_key,
                page_id=str(page_id) if page_id is not None else None,
                parent_page_id=str(parent_page_id) if parent_page_id is not None else None,
                title=resolved_title,
                labels=list(labels),
                force_overwrite=force_overwrite,
                add_source_banner=add_source_banner,
            )
            targets.append(target)
            link_map[rel_posix] = target

    return DiscoveryResult(
        config_path=cfg_path,
        repo_root=root_path,
        raw_config=raw_cfg,
        space_key=space_key,
        base_url_secret=base_url_secret,
        auth_secret=auth_secret,
        username_secret=str(username_secret) if username_secret else None,
        slack_webhook_secret=str(slack_secret) if slack_secret else None,
        defaults=defaults,
        targets=targets,
        link_map=link_map,
        warnings=warnings,
    )
