"""Unit tests for `.doc-sync.yml` JSON Schema validation and Discovery Engine."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.common.config import ConfigValidationError, load_and_validate_config
from tools.doc_sync.discovery import SCHEMA_PATH, DiscoveryError, discover_repository


class DiscoveryAndConfigTests(unittest.TestCase):
    def test_valid_config_and_glob_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "README.md").write_text("# Payments Service\n\nService overview.", encoding="utf-8")
            (root / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n- Initial release", encoding="utf-8")
            docs = root / "docs"
            docs.mkdir()
            (docs / "runbook.md").write_text("# Operations Runbook\n\nStep 1.", encoding="utf-8")
            (docs / "api.md").write_text("# REST API Reference\n\nEndpoints.", encoding="utf-8")

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
  add_source_banner: true
  on_missing_file: warn
mappings:
  - source: README.md
    confluence_page_id: "1001"
    title: "Payments Service — Overview"
    labels: [engineering, payments]
  - source: CHANGELOG.md
    confluence_page_id: "1002"
    force_overwrite: true
  - source: "docs/**/*.md"
    confluence_parent_page_id: "2000"
    title_from: h1
""",
                encoding="utf-8",
            )

            res = discover_repository(root, cfg)
            self.assertEqual(res.space_key, "ENG")
            self.assertEqual(len(res.targets), 4)
            self.assertIn("README.md", res.link_map)
            self.assertIn("docs/runbook.md", res.link_map)
            self.assertEqual(res.link_map["docs/runbook.md"].title, "Operations Runbook")
            self.assertTrue(res.link_map["CHANGELOG.md"].force_overwrite)
            self.assertFalse(res.link_map["README.md"].force_overwrite)

    def test_schema_rejects_raw_url_in_secret_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / ".doc-sync.yml"
            cfg.write_text(
                """
version: 1
confluence:
  space_key: ENG
  base_url_secret: "https://acme.atlassian.net/wiki"
  auth_secret: CONFLUENCE_PAT
mappings:
  - source: README.md
    confluence_page_id: "123"
""",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigValidationError):
                load_and_validate_config(cfg, SCHEMA_PATH)

    def test_schema_rejects_mapping_without_page_id_or_parent_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg = Path(tmpdir) / ".doc-sync.yml"
            cfg.write_text(
                """
version: 1
confluence:
  space_key: ENG
  base_url_secret: CONFLUENCE_BASE_URL
  auth_secret: CONFLUENCE_PAT
mappings:
  - source: README.md
    title: Missing Target Page ID
""",
                encoding="utf-8",
            )
            with self.assertRaises(ConfigValidationError):
                load_and_validate_config(cfg, SCHEMA_PATH)

    def test_missing_file_warn_vs_fail(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cfg = root / ".doc-sync.yml"
            cfg.write_text(
                """
version: 1
confluence:
  space_key: ENG
  base_url_secret: CONFLUENCE_BASE_URL
  auth_secret: CONFLUENCE_PAT
defaults:
  on_missing_file: fail
mappings:
  - source: NONEXISTENT.md
    confluence_page_id: "123"
""",
                encoding="utf-8",
            )
            with self.assertRaises(DiscoveryError):
                discover_repository(root, cfg)


if __name__ == "__main__":
    unittest.main()
