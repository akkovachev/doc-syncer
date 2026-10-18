"""End-to-end tests for Conflict Resolver, 409 Retry, HTTP Backoff, and Agentic Guardrails."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import requests

from tools.common.auth import ConfluenceCredentials
from tools.common.http import HttpClient, HttpError
from tools.doc_sync.agent import (
    draft_changelog_entry,
    evaluate_pr_completeness,
    generate_starter_config,
    summarize_xhtml_diff,
)
from tools.doc_sync.cli import run_sync_pipeline
from tools.doc_sync.confluence_client import ConfluenceClient, ConfluenceConflictError
from tools.doc_sync.state_cache import StateCache, compute_xhtml_hash


def _make_response(status_code: int, json_data: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None) -> requests.Response:
    resp = requests.Response()
    resp.status_code = status_code
    if headers:
        resp.headers.update(headers)
    body_str = json.dumps(json_data or {})
    resp._content = body_str.encode("utf-8")
    return resp


class SyncAndConflictTests(unittest.TestCase):
    def test_http_client_retries_429_and_503_with_backoff(self) -> None:
        mock_session = MagicMock(spec=requests.Session)
        mock_session.headers = {}
        mock_session.request.side_effect = [
            _make_response(429, {"message": "rate limited"}, headers={"Retry-After": "0.01"}),
            _make_response(503, {"message": "service unavailable"}),
            _make_response(200, {"ok": True}),
        ]
        slept: List[float] = []
        client = HttpClient(
            session=mock_session,
            sleep_fn=lambda d: slept.append(d),
            base_delay=0.01,
            max_delay=0.05,
        )
        resp = client.get("https://confluence.example.com/wiki/rest/api/content/123")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_session.request.call_count, 3)
        self.assertEqual(len(slept), 2)

    def test_confluence_client_retries_once_on_409_version_conflict(self) -> None:
        mock_session = MagicMock(spec=requests.Session)
        mock_session.headers = {}
        # Sequence:
        # 1. Initial PUT fails with 409
        # 2. Client GETs fresh page (now at version 4)
        # 3. Client retries PUT with version 5 -> succeeds (200)
        mock_session.request.side_effect = [
            _make_response(409, {"message": "Version conflict"}),
            _make_response(
                200,
                {
                    "id": "1001",
                    "title": "Overview",
                    "space": {"key": "ENG"},
                    "version": {"number": 4},
                    "body": {"storage": {"value": "<p>Old</p>"}},
                },
            ),
            _make_response(
                200,
                {
                    "id": "1001",
                    "title": "Overview",
                    "space": {"key": "ENG"},
                    "version": {"number": 5},
                    "body": {"storage": {"value": "<p>New</p>"}},
                },
            ),
        ]
        creds = ConfluenceCredentials(base_url="https://acme.atlassian.net/wiki", auth_token="secret-pat")
        http = HttpClient(session=mock_session, sleep_fn=lambda _: None)
        conf = ConfluenceClient(creds, http_client=http)

        updated = conf.update_page(
            page_id="1001",
            title="Overview",
            space_key="ENG",
            body_xhtml="<p>New</p>",
            current_version=3,
        )
        self.assertEqual(updated.version, 5)
        self.assertEqual(mock_session.request.call_count, 3)

    def test_sync_pipeline_skips_unchanged_and_detects_direct_confluence_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("# Overview\n\nUpdated local content.", encoding="utf-8")
            (root / "CHANGELOG.md").write_text("# Changelog\n\nNo change.", encoding="utf-8")
            cfg = root / ".doc-sync.yml"
            cfg.write_text(
                """
version: 1
confluence:
  space_key: ENG
  base_url_secret: CONFLUENCE_BASE_URL
  auth_secret: CONFLUENCE_PAT
defaults:
  force_overwrite: false
  add_source_banner: false
  agent_review: true
mappings:
  - source: README.md
    confluence_page_id: "1001"
    title: "Overview"
  - source: CHANGELOG.md
    confluence_page_id: "1002"
    title: "Changelog"
""",
                encoding="utf-8",
            )

            cache_path = root / ".doc-sync-cache.json"
            cache = StateCache(cache_path=cache_path)
            # Pre-populate cache:
            # - CHANGELOG.md has identical hash to current local file -> should skip without network call
            # - README.md was last synced at version 2, but Confluence is now at version 5 (edited directly!)
            changelog_xhtml = "<h1>Changelog</h1>\n<p>No change.</p>"
            cache.record_sync(
                source_rel_path="CHANGELOG.md",
                page_id="1002",
                content_hash=compute_xhtml_hash(changelog_xhtml),
                version=3,
                title="Changelog",
            )
            cache.record_sync(
                source_rel_path="README.md",
                page_id="1001",
                content_hash="old-hash",
                version=2,
                title="Overview",
            )
            cache.save()

            mock_session = MagicMock(spec=requests.Session)
            mock_session.headers = {}
            # Only README.md (page 1001) should trigger a network GET, returning version 5 (> cached v2)
            mock_session.request.side_effect = [
                _make_response(
                    200,
                    {
                        "id": "1001",
                        "title": "Overview",
                        "space": {"key": "ENG"},
                        "version": {"number": 5},
                        "body": {"storage": {"value": "<h1>Overview</h1>\n<p>Direct stakeholder edit in Confluence!</p>"}},
                    },
                )
            ]

            creds = ConfluenceCredentials(base_url="https://acme.atlassian.net/wiki", auth_token="pat")
            conf_client = ConfluenceClient(creds, http_client=HttpClient(session=mock_session))

            report = run_sync_pipeline(
                repo_root=root,
                config_file=cfg,
                cache_file=cache_path,
                client=conf_client,
            )

            self.assertEqual(report.checked_count, 2)
            self.assertEqual(report.skipped_count, 1)
            self.assertEqual(report.conflicted_count, 1)
            self.assertTrue(report.has_errors)
            # Verify CHANGELOG.md was skipped before network and README.md was blocked as conflicted
            by_src = {o.source_rel_path: o for o in report.outcomes}
            self.assertEqual(by_src["CHANGELOG.md"].status, "skipped_unchanged")
            self.assertEqual(by_src["README.md"].status, "conflicted")
            self.assertIsNotNone(by_src["README.md"].conflict_merge_suggestion)
            self.assertEqual(mock_session.request.call_count, 1)

    def test_agent_fail_open_and_pr_completeness_gate(self) -> None:
        # 1. Agent review fails open on exception/timeout
        review = summarize_xhtml_diff("<p>old</p>", "<p>new</p>", simulate_failure=True)
        self.assertIsNotNone(review.skipped_reason)
        self.assertIn("skipped", review.summary_text.lower())

        # 2. PR Completeness Gate flags code changes without doc updates
        gate_needs_docs = evaluate_pr_completeness(
            changed_files=["tools/doc-sync/converter.py"],
            commit_messages=["feat(converter): support callout panels", "fix(http): handle 429 retry-after"],
        )
        self.assertEqual(gate_needs_docs.verdict, "needs-doc-update")
        self.assertIn("Features (`feat`)", gate_needs_docs.draft_changelog_entry)
        self.assertIn("Bug Fixes (`fix`)", gate_needs_docs.draft_changelog_entry)

        # 3. PR Completeness Gate passes when README.md is updated alongside code
        gate_pass = evaluate_pr_completeness(
            changed_files=["tools/doc-sync/converter.py", "README.md"],
            commit_messages=["feat(converter): support callout panels"],
        )
        self.assertEqual(gate_pass.verdict, "pass")


if __name__ == "__main__":
    unittest.main()
