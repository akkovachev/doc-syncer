"""CLI and Sync Pipeline Orchestrator for Module 01: Doc-Sync Engine (`/tools/doc-sync/cli.py`).

Implements Sections 3.1–3.7, 4, and 5 of the Architecture Proposal:
- Subcommands:
  * `tools doc-sync sync`            — Full discovery, conflict check, XHTML conversion, and Confluence publish
  * `tools doc-sync dry-run`         — Runs pipeline without writing to Confluence; emits diffs & rendered XHTML
  * `tools doc-sync validate-config` — Fast JSON Schema + repository discovery validation for `.doc-sync.yml`
  * `tools doc-sync init`            — Copilot/Codex onboarding assistant to generate a starter `.doc-sync.yml`
  * `tools doc-sync pr-check`        — PR-time documentation completeness gate & CHANGELOG drafter
"""

from __future__ import annotations

import argparse
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from tools.common.auth import (
    ConfluenceCredentials,
    SecretResolutionError,
    resolve_confluence_credentials,
    resolve_secret,
)
from tools.common.config import ConfigValidationError, load_and_validate_config
from tools.common.http import HttpClient, HttpError
from tools.common.logging import ActionsLogger, get_logger

from .agent import (
    DiffReviewSummary,
    evaluate_pr_completeness,
    generate_starter_config,
    summarize_xhtml_diff,
)
from .confluence_client import ConfluenceClient, ConfluenceConflictError, ConfluencePage
from .converter import ConversionResult, convert_markdown_to_storage
from .discovery import SCHEMA_PATH, DiscoveryError, DiscoveryResult, SyncTarget, discover_repository
from .state_cache import StateCache, compute_xhtml_hash


@dataclass
class PageSyncOutcome:
    """Outcome record for a single mapped Markdown file."""

    source_rel_path: str
    title: str
    page_id: str
    status: str  # "updated" | "created" | "skipped_unchanged" | "dry_run" | "conflicted" | "failed"
    version: Optional[int] = None
    web_url: str = ""
    message: str = ""
    diff: str = ""
    agent_summary: Optional[str] = None
    conflict_merge_suggestion: Optional[str] = None


@dataclass
class SyncRunReport:
    """Aggregated report for an entire `doc-sync` invocation."""

    repo_name: str
    space_key: str
    dry_run: bool
    outcomes: List[PageSyncOutcome] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def checked_count(self) -> int:
        return len(self.outcomes)

    @property
    def updated_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status in ("updated", "created"))

    @property
    def skipped_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "skipped_unchanged")

    @property
    def dry_run_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "dry_run")

    @property
    def conflicted_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "conflicted")

    @property
    def failed_count(self) -> int:
        return sum(1 for o in self.outcomes if o.status == "failed")

    @property
    def has_errors(self) -> bool:
        return (self.conflicted_count + self.failed_count) > 0

    def to_markdown_summary(self) -> str:
        """Render the GitHub Actions job summary Markdown (Section 3.7)."""
        mode_badge = "🧪 DRY-RUN" if self.dry_run else "🚀 LIVE SYNC"
        status_badge = "❌ Action Required (Conflicts/Failures)" if self.has_errors else "✅ Success"
        lines = [
            f"## 📄 Doc-Sync Engine Report — `{self.repo_name}` ({mode_badge})",
            "",
            f"**Status:** {status_badge} | **Space:** `{self.space_key}` | "
            f"**Checked:** {self.checked_count} | **Updated:** {self.updated_count} | "
            f"**Skipped (Unchanged):** {self.skipped_count} | **Conflicted:** {self.conflicted_count} | "
            f"**Failed:** {self.failed_count}",
            "",
            "| Source File | Confluence Page | Status | Version | Details |",
            "| :--- | :--- | :--- | :---: | :--- |",
        ]

        status_icons = {
            "updated": "✅ Updated",
            "created": "✨ Created",
            "skipped_unchanged": "⏭️ Skipped (Unchanged)",
            "dry_run": "🧪 Dry-Run",
            "conflicted": "⚠️ Conflicted",
            "failed": "❌ Failed",
        }

        for o in self.outcomes:
            page_cell = f"[{o.title} ({o.page_id})]({o.web_url})" if o.web_url else f"{o.title} (`{o.page_id}`)"
            ver_cell = f"v{o.version}" if o.version is not None else "—"
            icon = status_icons.get(o.status, o.status)
            detail = o.message
            if o.agent_summary:
                detail = f"{detail}<br/>🤖 *AI Review:* {o.agent_summary}"
            lines.append(f"| `{o.source_rel_path}` | {page_cell} | {icon} | {ver_cell} | {detail} |")

        if self.warnings:
            lines.extend(["", "### ⚠️ Discovery Warnings"])
            for w in self.warnings:
                lines.append(f"- {w}")

        for o in self.outcomes:
            if o.diff and (o.status in ("conflicted", "dry_run")):
                lines.extend(
                    [
                        "",
                        f"### 🔍 Diff for `{o.source_rel_path}` (`{o.page_id}`)",
                        "```diff",
                        o.diff[:4000],
                        "```",
                    ]
                )
            if o.conflict_merge_suggestion:
                lines.extend(
                    [
                        "",
                        f"### 🤖 Copilot/Codex Conflict Merge Suggestion for `{o.source_rel_path}`",
                        "> Advisory suggestion only — human review required before merging.",
                        "```xml",
                        o.conflict_merge_suggestion[:4000],
                        "```",
                    ]
                )

        return "\n".join(lines) + "\n"


def _sync_single_target(
    target: SyncTarget,
    *,
    discovery: DiscoveryResult,
    repo_name: str,
    cache: StateCache,
    client: Optional[ConfluenceClient],
    dry_run: bool,
    drift_check: bool,
    force_all: bool,
    output_dir: Optional[Path],
    logger: ActionsLogger,
) -> PageSyncOutcome:
    """Execute Conversion -> Conflict Resolution -> Publish for a single `SyncTarget`."""
    conversion: ConversionResult = convert_markdown_to_storage(
        target.markdown_content,
        repo_name=repo_name,
        space_key=target.space_key,
        current_rel_path=target.source_rel_path,
        repo_root=discovery.repo_root,
        link_map=discovery.link_map,
        add_source_banner=target.add_source_banner,
    )
    local_xhtml = conversion.xhtml
    local_hash = compute_xhtml_hash(local_xhtml)

    if output_dir is not None:
        out_file = output_dir / f"{target.source_rel_path.replace('/', '__')}.xhtml"
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(local_xhtml, encoding="utf-8")

    cached_entry = cache.get(target.source_rel_path, page_id=target.page_id)
    effective_page_id = target.page_id or (cached_entry.page_id if cached_entry else None)

    # Step 3.3: Fast local hash check before touching the network
    if (
        not drift_check
        and not force_all
        and effective_page_id
        and cache.is_unchanged_locally(target.source_rel_path, local_xhtml, page_id=effective_page_id)
    ):
        ver = cached_entry.version if cached_entry else None
        web_url = client.build_page_web_url(effective_page_id, target.space_key) if client else ""
        logger.info(f"Skipping unchanged file '{target.source_rel_path}' (hash {local_hash[:12]} matches cache).")
        return PageSyncOutcome(
            source_rel_path=target.source_rel_path,
            title=target.title,
            page_id=effective_page_id,
            status="skipped_unchanged",
            version=ver,
            web_url=web_url,
            message="Local rendered XHTML hash unchanged since last sync",
        )

    # Offline dry-run mode (when no Confluence client / credentials are provided in dry-run)
    if client is None:
        placeholder_id = effective_page_id or f"parent:{target.parent_page_id}"
        logger.info(
            f"[Dry-Run Offline] Rendered '{target.source_rel_path}' -> {len(local_xhtml)} bytes XHTML "
            f"(hash {local_hash[:12]})."
        )
        return PageSyncOutcome(
            source_rel_path=target.source_rel_path,
            title=target.title,
            page_id=str(placeholder_id),
            status="dry_run",
            version=cached_entry.version if cached_entry else None,
            message=f"Rendered {len(local_xhtml)} bytes XHTML (sha256:{local_hash[:12]})",
        )

    # Resolve or create child page if mapped via `confluence_parent_page_id`
    remote_page: Optional[ConfluencePage] = None
    if effective_page_id:
        remote_page = client.get_page(effective_page_id)
    elif target.parent_page_id:
        remote_page = client.find_child_page_by_title(
            target.space_key,
            target.parent_page_id,
            target.title,
        )
        if remote_page is None:
            if dry_run:
                return PageSyncOutcome(
                    source_rel_path=target.source_rel_path,
                    title=target.title,
                    page_id=f"new-child-of-{target.parent_page_id}",
                    status="dry_run",
                    version=1,
                    message=f"Would create new child page under parent {target.parent_page_id}",
                )
            created_page = client.create_child_page(
                space_key=target.space_key,
                parent_page_id=target.parent_page_id,
                title=target.title,
                body_xhtml=local_xhtml,
            )
            if target.labels:
                client.sync_labels(created_page.page_id, target.labels)
            for attachment in conversion.attachments:
                client.upload_attachment(created_page.page_id, attachment)
            cache.record_sync(
                source_rel_path=target.source_rel_path,
                page_id=created_page.page_id,
                content_hash=local_hash,
                version=created_page.version,
                title=created_page.title,
            )
            logger.notice(
                f"Created new Confluence child page '{created_page.title}' (ID {created_page.page_id}, v1)",
                file=target.source_rel_path,
                title="Page Created",
            )
            return PageSyncOutcome(
                source_rel_path=target.source_rel_path,
                title=created_page.title,
                page_id=created_page.page_id,
                status="created",
                version=created_page.version,
                web_url=created_page.web_url,
                message=f"Created child page under {target.parent_page_id}",
            )

    assert remote_page is not None
    evaluation = cache.evaluate_remote_state(
        source_rel_path=target.source_rel_path,
        page_id=remote_page.page_id,
        local_xhtml=local_xhtml,
        remote_version=remote_page.version,
        remote_xhtml=remote_page.body_storage,
        force_overwrite=target.force_overwrite,
    )

    # Optional Section 5.2 Pre-Sync Agent Diff Review
    agent_review: Optional[DiffReviewSummary] = None
    if discovery.defaults.agent_review and evaluation.decision in ("update", "conflict"):
        agent_review = summarize_xhtml_diff(
            remote_page.body_storage,
            local_xhtml,
            is_conflict=(evaluation.decision == "conflict"),
            logger=logger,
        )

    if evaluation.decision == "conflict":
        logger.error(
            evaluation.reason,
            file=target.source_rel_path,
            title="Confluence Edit Conflict",
        )
        return PageSyncOutcome(
            source_rel_path=target.source_rel_path,
            title=target.title,
            page_id=remote_page.page_id,
            status="conflicted",
            version=remote_page.version,
            web_url=remote_page.web_url,
            message=evaluation.reason,
            diff=evaluation.diff,
            agent_summary=agent_review.summary_text if agent_review else None,
            conflict_merge_suggestion=agent_review.conflict_merge_suggestion if agent_review else None,
        )

    if evaluation.decision == "skip_unchanged":
        # Ensure cache stores current remote version if not yet recorded
        cache.record_sync(
            source_rel_path=target.source_rel_path,
            page_id=remote_page.page_id,
            content_hash=local_hash,
            version=remote_page.version,
            title=remote_page.title,
        )
        return PageSyncOutcome(
            source_rel_path=target.source_rel_path,
            title=remote_page.title,
            page_id=remote_page.page_id,
            status="skipped_unchanged",
            version=remote_page.version,
            web_url=remote_page.web_url,
            message=evaluation.reason,
        )

    if dry_run:
        logger.info(
            f"[Dry-Run] Would update Confluence page {remote_page.page_id} "
            f"('{target.title}') from v{remote_page.version} to v{remote_page.version + 1}."
        )
        return PageSyncOutcome(
            source_rel_path=target.source_rel_path,
            title=target.title,
            page_id=remote_page.page_id,
            status="dry_run",
            version=remote_page.version,
            web_url=remote_page.web_url,
            message=f"Would update v{remote_page.version} -> v{remote_page.version + 1}",
            diff=evaluation.diff,
            agent_summary=agent_review.summary_text if agent_review else None,
        )

    # Step 3.6: Publish (Safe Update) with optimistic locking & single 409 retry
    def _can_retry_409(fresh: ConfluencePage) -> bool:
        if target.force_overwrite:
            return True
        if cached_entry is not None and fresh.version > cached_entry.version:
            return False
        return True

    try:
        updated_page = client.update_page(
            page_id=remote_page.page_id,
            title=target.title,
            space_key=target.space_key,
            body_xhtml=local_xhtml,
            current_version=remote_page.version,
            allow_retry_on_409=True,
            conflict_check_fn=_can_retry_409,
        )
    except ConfluenceConflictError as conflict_exc:
        logger.error(
            str(conflict_exc),
            file=target.source_rel_path,
            title="Confluence 409 Version Conflict",
        )
        return PageSyncOutcome(
            source_rel_path=target.source_rel_path,
            title=target.title,
            page_id=remote_page.page_id,
            status="conflicted",
            version=conflict_exc.remote_version or remote_page.version,
            web_url=remote_page.web_url,
            message=str(conflict_exc),
            diff=evaluation.diff,
            agent_summary=agent_review.summary_text if agent_review else None,
        )

    if target.labels:
        client.sync_labels(updated_page.page_id, target.labels)
    for attachment in conversion.attachments:
        client.upload_attachment(updated_page.page_id, attachment)

    cache.record_sync(
        source_rel_path=target.source_rel_path,
        page_id=updated_page.page_id,
        content_hash=local_hash,
        version=updated_page.version,
        title=updated_page.title,
    )
    logger.notice(
        f"Synced '{target.source_rel_path}' -> Confluence page {updated_page.page_id} (v{updated_page.version})",
        file=target.source_rel_path,
        title="Page Synced",
    )
    return PageSyncOutcome(
        source_rel_path=target.source_rel_path,
        title=updated_page.title,
        page_id=updated_page.page_id,
        status="updated",
        version=updated_page.version,
        web_url=updated_page.web_url,
        message=f"Updated to v{updated_page.version}",
        agent_summary=agent_review.summary_text if agent_review else None,
    )


def run_sync_pipeline(
    *,
    repo_root: Path,
    config_file: Path,
    cache_file: Path,
    repo_name: Optional[str] = None,
    dry_run: bool = False,
    drift_check: bool = False,
    force_all: bool = False,
    output_dir: Optional[Path] = None,
    client: Optional[ConfluenceClient] = None,
    env: Optional[Dict[str, str]] = None,
    logger: Optional[ActionsLogger] = None,
) -> SyncRunReport:
    """Execute the end-to-end Doc-Sync workflow and return a `SyncRunReport`."""
    log = logger or get_logger("doc-sync")
    discovery = discover_repository(repo_root, config_file, logger=log)

    effective_repo_name = (
        repo_name
        or (env if env is not None else os.environ).get("GITHUB_REPOSITORY")
        or discovery.repo_root.name
    )

    cache = StateCache.load(cache_file)

    if client is None:
        try:
            creds: ConfluenceCredentials = resolve_confluence_credentials(
                discovery.raw_config["confluence"],
                env=env,
                logger=log,
            )
            client = ConfluenceClient(creds, logger=log)
        except SecretResolutionError as exc:
            if dry_run:
                log.info(
                    f"Confluence secrets not set in environment ({exc}); "
                    "running dry-run in local/offline rendering mode."
                )
                client = None
            else:
                raise

    report = SyncRunReport(
        repo_name=effective_repo_name,
        space_key=discovery.space_key,
        dry_run=dry_run,
        warnings=list(discovery.warnings),
    )

    for target in discovery.targets:
        try:
            outcome = _sync_single_target(
                target,
                discovery=discovery,
                repo_name=effective_repo_name,
                cache=cache,
                client=client,
                dry_run=dry_run,
                drift_check=drift_check,
                force_all=force_all,
                output_dir=output_dir,
                logger=log,
            )
            report.outcomes.append(outcome)
        except (HttpError, Exception) as exc:
            log.error(
                f"Failed to sync '{target.source_rel_path}': {exc}",
                file=target.source_rel_path,
                title="Sync Failure",
            )
            report.outcomes.append(
                PageSyncOutcome(
                    source_rel_path=target.source_rel_path,
                    title=target.title,
                    page_id=target.page_id or f"parent:{target.parent_page_id}",
                    status="failed",
                    message=str(exc),
                )
            )

    if not dry_run:
        cache.save()

    summary_md = report.to_markdown_summary()
    log.write_job_summary(summary_md)

    # Optional Slack notification on conflict or failure (Section 4)
    if report.has_errors and discovery.slack_webhook_secret:
        webhook_url = resolve_secret(
            discovery.slack_webhook_secret,
            required=False,
            mask=True,
            env=env,
            logger=log,
        )
        if webhook_url:
            http_client = client.http if client is not None else HttpClient(logger=log)
            alert_text = (
                f":warning: *Doc-Sync Alert* for `{effective_repo_name}` (Space `{discovery.space_key}`): "
                f"{report.conflicted_count} conflicted, {report.failed_count} failed."
            )
            http_client.post_slack_webhook(webhook_url, alert_text)

    return report


def _handle_sync_cmd(args: argparse.Namespace) -> int:
    log = get_logger("doc-sync", verbose=getattr(args, "verbose", False))
    repo_root = Path(args.repo_root).resolve()
    config_file = Path(args.config)
    cache_file = Path(args.cache_file)
    if not cache_file.is_absolute():
        cache_file = repo_root / cache_file
    output_dir = Path(args.output_dir).resolve() if getattr(args, "output_dir", None) else None

    try:
        report = run_sync_pipeline(
            repo_root=repo_root,
            config_file=config_file,
            cache_file=cache_file,
            repo_name=getattr(args, "repo_name", None),
            dry_run=bool(getattr(args, "dry_run", False)),
            drift_check=bool(getattr(args, "drift_check", False)),
            force_all=bool(getattr(args, "force_all", False)),
            output_dir=output_dir,
            logger=log,
        )
    except (ConfigValidationError, DiscoveryError, SecretResolutionError) as exc:
        log.error(str(exc), title="Configuration / Setup Error")
        return 1

    print(report.to_markdown_summary())
    return 1 if report.has_errors else 0


def _handle_validate_config_cmd(args: argparse.Namespace) -> int:
    log = get_logger("doc-sync", verbose=getattr(args, "verbose", False))
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = (repo_root / config_path).resolve()

    try:
        load_and_validate_config(config_path, SCHEMA_PATH)
        if args.check_files:
            res = discover_repository(repo_root, config_path, logger=log)
            log.info(
                f"Configuration '{config_path.name}' is valid. "
                f"Discovered {len(res.targets)} Markdown target(s) in space '{res.space_key}'."
            )
        else:
            log.info(f"Configuration '{config_path.name}' passed JSON Schema validation.")
        return 0
    except (ConfigValidationError, DiscoveryError) as exc:
        log.error(str(exc), file=str(args.config), title="Invalid .doc-sync.yml")
        return 1


def _handle_init_cmd(args: argparse.Namespace) -> int:
    log = get_logger("doc-sync", verbose=getattr(args, "verbose", False))
    repo_root = Path(args.repo_root).resolve()
    out_path = repo_root / args.output
    if out_path.exists() and not args.overwrite:
        log.error(
            f"File '{out_path}' already exists. Pass --overwrite to replace it.",
            title="Init Aborted",
        )
        return 1

    yaml_text = generate_starter_config(repo_root, space_key=args.space_key)
    out_path.write_text(yaml_text, encoding="utf-8")
    log.notice(f"Generated starter configuration at '{out_path}'.", file=str(args.output), title="Config Initialized")
    return 0


def _handle_pr_check_cmd(args: argparse.Namespace) -> int:
    log = get_logger("doc-sync", verbose=getattr(args, "verbose", False))
    repo_root = Path(args.repo_root).resolve()

    changed_files: List[str] = list(args.changed_files or [])
    commit_messages: List[str] = list(args.commits or [])

    if not changed_files and args.base_ref:
        try:
            proc = subprocess.run(
                ["git", "diff", "--name-only", f"{args.base_ref}...HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                changed_files = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        except Exception:
            pass

    if not commit_messages and args.base_ref:
        try:
            proc = subprocess.run(
                ["git", "log", "--format=%s", f"{args.base_ref}..HEAD"],
                cwd=str(repo_root),
                capture_output=True,
                text=True,
                check=False,
            )
            if proc.returncode == 0:
                commit_messages = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
        except Exception:
            pass

    report = evaluate_pr_completeness(changed_files, commit_messages)
    log.write_job_summary(report.markdown_comment)
    print(report.markdown_comment)

    if report.verdict != "pass" and args.strict:
        log.error(report.summary, title="PR Documentation Completeness Gate")
        return 1
    if report.verdict != "pass":
        log.warning(report.summary, title="PR Documentation Completeness Gate (Advisory)")
    return 0


def register_doc_sync_subcommands(subparsers: argparse._SubParsersAction) -> None:
    """Attach `doc-sync` and its subcommands (`sync`, `dry-run`, `validate-config`, `init`, `pr-check`)."""
    doc_parser = subparsers.add_parser(
        "doc-sync",
        help="Module 01: Synchronize repository Markdown documentation to Confluence",
        description="Module 01: Doc-Sync Engine — Markdown to Confluence Storage Format publisher.",
    )
    doc_sub = doc_parser.add_subparsers(dest="doc_sync_command", metavar="<subcommand>")

    def _add_common_sync_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--config", default=".doc-sync.yml", help="Path to .doc-sync.yml config file")
        p.add_argument("--repo-root", default=".", help="Path to repository root")
        p.add_argument("--repo-name", default=None, help="Repository display name (e.g. org/payments-service)")
        p.add_argument(
            "--cache-file",
            default=".doc-sync-cache.json",
            help="Path to JSON state cache file (cached between Actions runs)",
        )
        p.add_argument(
            "--output-dir",
            default=None,
            help="Optional directory to write rendered Confluence Storage Format (.xhtml) files",
        )
        p.add_argument(
            "--drift-check",
            action="store_true",
            help="Check remote Confluence versions even if local XHTML hashes are unchanged",
        )
        p.add_argument(
            "--force-all",
            action="store_true",
            help="Bypass local hash cache and inspect all mapped pages in Confluence",
        )
        p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")

    # 1. `sync`
    sync_p = doc_sub.add_parser("sync", help="Discover, convert, and publish changed Markdown files to Confluence")
    _add_common_sync_args(sync_p)
    sync_p.add_argument("--dry-run", action="store_true", help="Run without writing to Confluence")
    sync_p.set_defaults(handler=_handle_sync_cmd)

    # 2. `dry-run`
    dry_p = doc_sub.add_parser("dry-run", help="Render XHTML and compute diffs without writing to Confluence")
    _add_common_sync_args(dry_p)
    dry_p.set_defaults(handler=lambda a: _handle_sync_cmd(argparse.Namespace(**{**vars(a), "dry_run": True})))

    # 3. `validate-config`
    val_p = doc_sub.add_parser("validate-config", help="Validate .doc-sync.yml against JSON Schema")
    val_p.add_argument("--config", default=".doc-sync.yml", help="Path to .doc-sync.yml config file")
    val_p.add_argument("--repo-root", default=".", help="Path to repository root")
    val_p.add_argument(
        "--check-files",
        action="store_true",
        default=True,
        help="Also verify that mapped Markdown files exist in the repository",
    )
    val_p.add_argument(
        "--schema-only",
         dest="check_files",
        action="store_false",
        help="Validate YAML schema only without checking local files",
    )
    val_p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    val_p.set_defaults(handler=_handle_validate_config_cmd)

    # 4. `init` (Section 5.3)
    init_p = doc_sub.add_parser("init", help="Scan repository and generate a starter .doc-sync.yml config")
    init_p.add_argument("--repo-root", default=".", help="Path to repository root")
    init_p.add_argument("--space-key", default="ENG", help="Default Confluence space key")
    init_p.add_argument("--output", default=".doc-sync.yml", help="Output filename")
    init_p.add_argument("--overwrite", action="store_true", help="Overwrite existing .doc-sync.yml if present")
    init_p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    init_p.set_defaults(handler=_handle_init_cmd)

    # 5. `pr-check` (Section 5.1)
    pr_p = doc_sub.add_parser(
        "pr-check",
        help="Run PR-time documentation completeness gate and draft CHANGELOG entry",
    )
    pr_p.add_argument("--repo-root", default=".", help="Path to repository root")
    pr_p.add_argument("--base-ref", default="origin/main", help="Git base ref for diffing changed files")
    pr_p.add_argument("--changed-files", nargs="*", default=None, help="Explicit list of changed file paths")
    pr_p.add_argument("--commits", nargs="*", default=None, help="Explicit list of commit subject lines")
    pr_p.add_argument(
        "--strict",
        action="store_true",
        help="Exit with non-zero code if code changed without documentation updates",
    )
    pr_p.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging")
    pr_p.set_defaults(handler=_handle_pr_check_cmd)

    doc_parser.set_defaults(handler=lambda _: doc_parser.print_help() or 1)
