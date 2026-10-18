"""Snapshot and unit tests for `tools/doc-sync/converter.py` (Section 3.4 & Section 6.2)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.doc_sync.converter import convert_markdown_to_storage
from tools.doc_sync.discovery import SyncTarget


class ConverterSnapshotTests(unittest.TestCase):
    def test_source_banner_injected_when_enabled(self) -> None:
        res = convert_markdown_to_storage(
            "# Hello World\n\nWelcome to the service.",
            repo_name="org/payments-service",
            current_rel_path="README.md",
            add_source_banner=True,
        )
        self.assertIn(
            "This page is auto-generated from <code>org/payments-service/README.md</code>",
            res.xhtml,
        )
        self.assertIn("<h1>Hello World</h1>", res.xhtml)
        self.assertIn("<p>Welcome to the service.</p>", res.xhtml)

    def test_fenced_code_block_renders_confluence_code_macro(self) -> None:
        md = "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```"
        res = convert_markdown_to_storage(md, add_source_banner=False)
        expected = (
            '<ac:structured-macro ac:name="code">'
            '<ac:parameter ac:name="language">python</ac:parameter>'
            "<ac:plain-text-body><![CDATA[def add(a: int, b: int) -> int:\n    return a + b]]></ac:plain-text-body>"
            "</ac:structured-macro>"
        )
        self.assertEqual(res.xhtml, expected)

    def test_gfm_alerts_render_confluence_panels(self) -> None:
        md = (
            "> [!NOTE]\n"
            "> Internal service note.\n\n"
            "> [!WARNING]\n"
            "> Breaking API change ahead.\n\n"
            "> [!TIP]\n"
            "> Use dry-run first.\n"
        )
        res = convert_markdown_to_storage(md, add_source_banner=False)
        self.assertIn(
            '<ac:structured-macro ac:name="info"><ac:rich-text-body><p>Internal service note.</p></ac:rich-text-body></ac:structured-macro>',
            res.xhtml,
        )
        self.assertIn(
            '<ac:structured-macro ac:name="warning"><ac:rich-text-body><p>Breaking API change ahead.</p></ac:rich-text-body></ac:structured-macro>',
            res.xhtml,
        )
        self.assertIn(
            '<ac:structured-macro ac:name="tip"><ac:rich-text-body><p>Use dry-run first.</p></ac:rich-text-body></ac:structured-macro>',
            res.xhtml,
        )

    def test_table_renders_native_confluence_table(self) -> None:
        md = (
            "| Field | Required | Purpose |\n"
            "| --- | --- | --- |\n"
            "| `space_key` | Yes | Target space |\n"
            "| `force_overwrite` | No | Escape hatch |\n"
        )
        res = convert_markdown_to_storage(md, add_source_banner=False)
        self.assertEqual(
            res.xhtml,
            "<table>"
            "<thead><tr><th>Field</th><th>Required</th><th>Purpose</th></tr></thead>"
            "<tbody>"
            "<tr><td><code>space_key</code></td><td>Yes</td><td>Target space</td></tr>"
            "<tr><td><code>force_overwrite</code></td><td>No</td><td>Escape hatch</td></tr>"
            "</tbody>"
            "</table>",
        )

    def test_relative_links_to_synced_markdown_rewritten_to_confluence_page_links(self) -> None:
        link_map = {
            "CHANGELOG.md": SyncTarget(
                source_rel_path="CHANGELOG.md",
                abs_path=Path("CHANGELOG.md"),
                markdown_content="# Changelog",
                space_key="ENG",
                page_id="123456790",
                title="Payments Service — Changelog",
            ),
            "docs/arch.md": SyncTarget(
                source_rel_path="docs/arch.md",
                abs_path=Path("docs/arch.md"),
                markdown_content="# Architecture",
                space_key="ENG",
                page_id="123456791",
                title="System Architecture",
            ),
        }
        md = "See the [Release Notes](./CHANGELOG.md) and [Arch Overview](docs/arch.md#flow)."
        res = convert_markdown_to_storage(
            md,
            space_key="ENG",
            current_rel_path="README.md",
            link_map=link_map,
            add_source_banner=False,
        )
        self.assertIn(
            '<ac:link><ri:page ri:space-key="ENG" ri:content-title="Payments Service — Changelog" />'
            "<ac:plain-text-link-body><![CDATA[Release Notes]]></ac:plain-text-link-body></ac:link>",
            res.xhtml,
        )
        self.assertIn(
            '<ac:link ac:anchor="flow"><ri:page ri:space-key="ENG" ri:content-title="System Architecture" />'
            "<ac:plain-text-link-body><![CDATA[Arch Overview]]></ac:plain-text-link-body></ac:link>",
            res.xhtml,
        )

    def test_local_and_remote_images_and_toc(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            img_dir = root / "assets"
            img_dir.mkdir()
            img_file = img_dir / "diagram.png"
            img_file.write_bytes(b"\x89PNG\r\n\x1a\n")

            md = (
                "[[_TOC_]]\n\n"
                "![Architecture Diagram](assets/diagram.png)\n\n"
                "![Remote Badge](https://example.com/badge.svg)\n"
            )
            res = convert_markdown_to_storage(
                md,
                repo_root=root,
                current_rel_path="README.md",
                add_source_banner=False,
            )
            self.assertIn('<ac:structured-macro ac:name="toc" />', res.xhtml)
            self.assertIn(
                '<ac:image ac:alt="Architecture Diagram"><ri:attachment ri:filename="diagram.png" /></ac:image>',
                res.xhtml,
            )
            self.assertIn(
                '<ac:image ac:alt="Remote Badge"><ri:url ri:value="https://example.com/badge.svg" /></ac:image>',
                res.xhtml,
            )
            self.assertEqual(len(res.attachments), 1)
            self.assertEqual(res.attachments[0].name, "diagram.png")


if __name__ == "__main__":
    unittest.main()
