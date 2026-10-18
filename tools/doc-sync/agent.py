"""Agentic Integration Layer — GitHub Copilot / Codex (`/tools/doc-sync/agent.py`).

Implements Section 5 of the Architecture Proposal:
- 5.1 PR-Time Documentation Completeness Gate (`evaluate_pr_completeness` & `draft_changelog_entry`):
      Inspects changed files / commits on a PR, checks whether public behavior/code/config
      changed without corresponding updates to `README.md` or `CHANGELOG.md`, and drafts a
      conventional-commit `CHANGELOG.md` entry (`feat`/`fix`/`chore`).
- 5.2 Pre-Sync Diff Review (`summarize_xhtml_diff` & `propose_conflict_merge`):
      Summarizes local-vs-Confluence XHTML diffs in plain language for the job summary, and
      on flagged conflicts drafts an advisory merged reconciliation suggestion. Strictly fails
      open: any error or timeout logs a note and allows the deterministic engine path to proceed.
- 5.3 Config Onboarding Assistance (`generate_starter_config`):
      Scans a repository for Markdown files (`README.md`, `CHANGELOG.md`, `docs/**/*.md`),
      infers titles and labels, and produces a starter `.doc-sync.yml`.
- Guardrail: All outputs are strictly advisory suggestions for human review — never auto-merges
  or overwrites Confluence pages.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import yaml

from tools.common.logging import ActionsLogger, get_logger
from .discovery import derive_title

_HEADING_TAG_RE = re.compile(r"<h[1-6][^>]*>(.*?)</h[1-6]>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_CONVENTIONAL_COMMIT_RE = re.compile(
    r"^(feat|fix|perf|refactor|docs|chore|build|ci|test)(?:\(([^)]+)\))?(!)?:\s*(.+)$",
    re.IGNORECASE,
)


@dataclass
class PRCompletenessReport:
    """Result of the PR-time documentation completeness gate (Section 5.1)."""

    verdict: str  # "pass" | "needs-doc-update"
    summary: str
    changed_code_files: List[str] = field(default_factory=list)
    changed_doc_files: List[str] = field(default_factory=list)
    draft_changelog_entry: str = ""
    markdown_comment: str = ""


@dataclass
class DiffReviewSummary:
    """Result of the pre-sync diff review step (Section 5.2)."""

    summary_text: str
    conflict_merge_suggestion: Optional[str] = None
    skipped_reason: Optional[str] = None


def _strip_tags(xhtml: str) -> str:
    return _HTML_TAG_RE.sub("", xhtml).strip()


def _extract_headings_from_xhtml(xhtml: str) -> List[str]:
    return [_strip_tags(m.group(1)) for m in _HEADING_TAG_RE.finditer(xhtml) if _strip_tags(m.group(1))]


def summarize_xhtml_diff(
    remote_xhtml: str,
    local_xhtml: str,
    *,
    is_conflict: bool = False,
    simulate_failure: bool = False,
    logger: Optional[ActionsLogger] = None,
) -> DiffReviewSummary:
    """Summarize the difference between current Confluence XHTML and local rendered XHTML.

    Fails open per Section 5.2: if any exception occurs, returns a `DiffReviewSummary`
    with `skipped_reason` set so the deterministic sync pipeline is never blocked.
    """
    log = logger or get_logger("agent")
    try:
        if simulate_failure:
            raise RuntimeError("Simulated Copilot/Codex agent timeout")

        old_headings = _extract_headings_from_xhtml(remote_xhtml)
        new_headings = _extract_headings_from_xhtml(local_xhtml)
        added_sections = [h for h in new_headings if h not in old_headings]
        removed_sections = [h for h in old_headings if h not in new_headings]

        parts: List[str] = []
        if added_sections:
            quoted = ", ".join(f"'{s}'" for s in added_sections[:3])
            parts.append(f"adds section(s) {quoted}")
        if removed_sections:
            quoted = ", ".join(f"'{s}'" for s in removed_sections[:3])
            parts.append(f"removes section(s) {quoted}")

        old_lines = [ln.strip() for ln in remote_xhtml.splitlines() if ln.strip()]
        new_lines = [ln.strip() for ln in local_xhtml.splitlines() if ln.strip()]
        delta_lines = len(new_lines) - len(old_lines)

        if parts:
            summary = f"This sync {' and '.join(parts)} ({delta_lines:+d} XHTML lines)."
        else:
            summary = f"This sync updates body content across existing sections ({delta_lines:+d} XHTML lines)."

        merge_suggestion: Optional[str] = None
        if is_conflict:
            merge_suggestion = propose_conflict_merge(remote_xhtml, local_xhtml)

        return DiffReviewSummary(
            summary_text=summary,
            conflict_merge_suggestion=merge_suggestion,
        )
    except Exception as exc:
        log.warning(f"AI pre-sync diff review skipped (fail-open): {exc}", title="Agent Review Skipped")
        return DiffReviewSummary(
            summary_text="AI review was skipped due to agent error/timeout; deterministic diff used.",
            skipped_reason=str(exc),
        )


def propose_conflict_merge(remote_xhtml: str, local_xhtml: str) -> str:
    """Draft an advisory reconciled version combining direct Confluence edits with repo edits."""
    remote_lines = remote_xhtml.strip().splitlines()
    local_lines = local_xhtml.strip().splitlines()
    remote_only = [ln for ln in remote_lines if ln not in local_lines]

    suggestion_lines = [
        "<!-- ADVISORY CONFLICT RECONCILIATION SUGGESTION (Human Approval Required) -->",
        "<!-- Base: Local repository rendered XHTML -->",
        *local_lines,
    ]
    if remote_only:
        suggestion_lines.append("<!-- Preserved lines from direct Confluence edit: -->")
        suggestion_lines.extend(remote_only)
    return "\n".join(suggestion_lines)


def draft_changelog_entry(
    commit_messages: Sequence[str],
    *,
    version_heading: str = "Unreleased",
) -> str:
    """Generate a draft CHANGELOG.md section from conventional commit messages or PR titles."""
    buckets: Dict[str, List[str]] = {
        "Features (`feat`)": [],
        "Bug Fixes (`fix`)": [],
        "Maintenance (`chore` / other)": [],
    }

    for raw_msg in commit_messages:
        first_line = raw_msg.strip().splitlines()[0].strip() if raw_msg.strip() else ""
        if not first_line:
            continue
        match = _CONVENTIONAL_COMMIT_RE.match(first_line)
        if match:
            ctype = match.group(1).lower()
            scope = match.group(2)
            breaking = match.group(3)
            desc = match.group(4).strip()
            prefix = f"**{scope}**: " if scope else ""
            bang = " **[BREAKING]**" if breaking else ""
            formatted = f"- {prefix}{desc}{bang}"
            if ctype == "feat":
                buckets["Features (`feat`)"].append(formatted)
            elif ctype == "fix":
                buckets["Bug Fixes (`fix`)"].append(formatted)
            else:
                buckets["Maintenance (`chore` / other)"].append(formatted)
        else:
            buckets["Maintenance (`chore` / other)"].append(f"- {first_line}")

    lines = [f"## [{version_heading}]"]
    any_entries = False
    for section_title, items in buckets.items():
        if items:
            any_entries = True
            lines.append(f"### {section_title}")
            lines.extend(items)
            lines.append("")

    if not any_entries:
        lines.append("- Maintenance and internal updates.")

    return "\n".join(lines).strip()


def evaluate_pr_completeness(
    changed_files: Sequence[str],
    commit_messages: Sequence[str] = (),
) -> PRCompletenessReport:
    """Evaluate whether a PR touching code/config also updates documentation (Section 5.1)."""
    norm_files = [f.replace("\\", "/").strip() for f in changed_files if f.strip()]
    doc_files = [
        f
        for f in norm_files
        if f.lower().endswith((".md", ".markdown")) or f.startswith("docs/")
    ]
    code_files = [
        f
        for f in norm_files
        if f not in doc_files
        and not f.startswith((".github/", "tests/", "tools/doc-sync/tests/"))
        and not f.endswith((".lock", ".gitignore"))
    ]

    draft_changelog = draft_changelog_entry(commit_messages)

    if code_files and not doc_files:
        verdict = "needs-doc-update"
        summary = (
            f"PR modifies {len(code_files)} code/config file(s) ({', '.join(code_files[:3])}) "
            "without updating `README.md`, `CHANGELOG.md`, or `docs/**`."
        )
    else:
        verdict = "pass"
        summary = (
            f"Documentation completeness check passed "
            f"({len(doc_files)} doc file(s) updated alongside {len(code_files)} code file(s))."
        )

    badge = "✅ **PASS**" if verdict == "pass" else "⚠️ **NEEDS DOC UPDATE**"
    comment_lines = [
        "### 🤖 Copilot / Codex PR Documentation Completeness Gate",
        f"**Verdict:** {badge} (`{verdict}`)",
        "",
        f"{summary}",
        "",
        "#### Suggested `CHANGELOG.md` Entry",
        "```markdown",
        draft_changelog,
        "```",
    ]

    return PRCompletenessReport(
        verdict=verdict,
        summary=summary,
        changed_code_files=code_files,
        changed_doc_files=doc_files,
        draft_changelog_entry=draft_changelog,
        markdown_comment="\n".join(comment_lines),
    )


def generate_starter_config(
    repo_root: Path,
    *,
    space_key: str = "ENG",
) -> str:
    """Scan a repository for Markdown files and propose a starter `.doc-sync.yml` (Section 5.3)."""
    root = repo_root.resolve()
    repo_slug = root.name.lower().replace("_", "-")
    mappings: List[Dict[str, object]] = []

    readme_path = root / "README.md"
    if readme_path.exists():
        content = readme_path.read_text(encoding="utf-8")
        title = derive_title("README.md", content, title_from="h1")
        mappings.append(
            {
                "source": "README.md",
                "confluence_page_id": "123456789",
                "title": title,
                "labels": ["engineering", repo_slug],
            }
        )

    changelog_path = root / "CHANGELOG.md"
    if changelog_path.exists():
        mappings.append(
            {
                "source": "CHANGELOG.md",
                "confluence_page_id": "123456790",
                "force_overwrite": True,
            }
        )

    docs_dir = root / "docs"
    if docs_dir.exists() and any(docs_dir.rglob("*.md")):
        mappings.append(
            {
                "source": "docs/**/*.md",
                "confluence_parent_page_id": "123400000",
                "title_from": "h1",
            }
        )

    # Fallback if no standard files exist yet
    if not mappings:
        mappings.append(
            {
                "source": "README.md",
                "confluence_page_id": "123456789",
                "title": f"{root.name} — Overview",
                "labels": ["engineering", repo_slug],
            }
        )

    config_doc = {
        "version": 1,
        "confluence": {
            "space_key": space_key,
            "base_url_secret": "CONFLUENCE_BASE_URL",
            "auth_secret": "CONFLUENCE_PAT",
        },
        "defaults": {
            "force_overwrite": False,
            "add_source_banner": True,
            "on_missing_file": "warn",
            "agent_review": True,
        },
        "mappings": mappings,
        "notifications": {
            "slack_webhook_secret": "DOC_SYNC_SLACK_WEBHOOK",
        },
    }

    header_comment = (
        "# .doc-sync.yml — Generated by `tools doc-sync init` (Copilot/Codex Onboarding Assistant)\n"
        "# Review page IDs below before enabling live Confluence sync.\n"
    )
    return header_comment + yaml.safe_dump(config_doc, sort_keys=False)
