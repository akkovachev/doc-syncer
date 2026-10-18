"""Markdown → Confluence Storage Format (XHTML) Converter (`/tools/doc-sync/converter.py`).

Implements Section 3.4 of the Architecture Proposal:
- Converts CommonMark + GitHub Flavored Markdown (GFM) into Confluence Storage Format (XHTML).
- Mapped constructs:
  * Fenced code blocks -> `<ac:structured-macro ac:name="code">` with `<ac:parameter ac:name="language">`
    and `<ac:plain-text-body><![CDATA[...]]></ac:plain-text-body>`
  * Tables -> native Confluence XHTML `<table><thead>...</thead><tbody>...</tbody></table>`
  * GitHub-flavored alerts/callouts (`> [!NOTE]`, `> [!TIP]`, `> [!IMPORTANT]`, `> [!WARNING]`, `> [!CAUTION]`)
    -> Confluence `info` / `tip` / `note` / `warning` structured macros
  * Table of Contents (`[TOC]` / `[[_TOC_]]`) -> `<ac:structured-macro ac:name="toc" />`
  * Relative links to other synced Markdown files -> `<ac:link><ri:page ... /></ac:link>`
    using the Discovery Engine's mapping table so cross-doc links keep working after sync
  * Local images -> recorded as page attachments to upload and referenced via
    `<ac:image><ri:attachment ri:filename="..." /></ac:image>`
  * Source banner panel injected at the top of every synced page (`add_source_banner: true`).
"""

from __future__ import annotations

import html
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .discovery import SyncTarget

# Mapping of GitHub-style alert tags `> [!TYPE]` to Confluence macro names
ALERT_MACRO_MAP: Dict[str, str] = {
    "NOTE": "info",
    "INFO": "info",
    "TIP": "tip",
    "IMPORTANT": "note",
    "WARNING": "warning",
    "CAUTION": "warning",
}

_ALERT_HEADER_RE = re.compile(r"^\s*\[!(NOTE|INFO|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(.*)$", re.IGNORECASE)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")
_FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*([^\s`~]*).*$")
_HR_RE = re.compile(r"^\s*(?:-\s*-\s*-[-\s]*|\*\s*\*\s*\*[\*\s]*|_\s*_\s*_[_\s]*)$")
_TOC_RE = re.compile(r"^\s*(?:\[\[?_?TOC_?\]\]?)\s*$", re.IGNORECASE)
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)\d+\.\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?\s*$")


@dataclass
class ConversionResult:
    """Rendered Confluence Storage Format payload and discovered local image attachments."""

    xhtml: str
    attachments: List[Path] = field(default_factory=list)


def _escape_xml(text: str) -> str:
    return html.escape(text, quote=True)


def _escape_cdata(text: str) -> str:
    """Safely escape `]]>` inside a CDATA section."""
    return text.replace("]]>", "]]]]><![CDATA[>")


def build_source_banner(repo_name: str, source_rel_path: str) -> str:
    """Build the Confluence info panel banner injected at the top of synced pages (Section 3.4)."""
    full_ref = f"{repo_name}/{source_rel_path}" if repo_name else source_rel_path
    escaped_ref = _escape_xml(full_ref)
    return (
        '<ac:structured-macro ac:name="info">'
        "<ac:rich-text-body>"
        f"<p>This page is auto-generated from <code>{escaped_ref}</code> — "
        "edits here will be overwritten on next sync. Edit the source file instead.</p>"
        "</ac:rich-text-body>"
        "</ac:structured-macro>"
    )


def build_code_macro(code: str, language: Optional[str] = None) -> str:
    """Build a Confluence Storage Format `code` structured macro."""
    lang_param = ""
    if language:
        clean_lang = _escape_xml(language.strip().lower())
        lang_param = f'<ac:parameter ac:name="language">{clean_lang}</ac:parameter>'
    safe_code = _escape_cdata(code.rstrip("\r\n"))
    return (
        '<ac:structured-macro ac:name="code">'
        f"{lang_param}"
        f"<ac:plain-text-body><![CDATA[{safe_code}]]></ac:plain-text-body>"
        "</ac:structured-macro>"
    )


def build_panel_macro(macro_name: str, inner_xhtml: str, title: Optional[str] = None) -> str:
    """Build a Confluence `info` / `tip` / `note` / `warning` panel macro."""
    title_param = f'<ac:parameter ac:name="title">{_escape_xml(title)}</ac:parameter>' if title else ""
    return (
        f'<ac:structured-macro ac:name="{_escape_xml(macro_name)}">'
        f"{title_param}"
        f"<ac:rich-text-body>{inner_xhtml}</ac:rich-text-body>"
        "</ac:structured-macro>"
    )


class ConfluenceConverter:
    """Stateful converter for a single Markdown file within a repository context."""

    def __init__(
        self,
        *,
        repo_name: str = "",
        space_key: str = "ENG",
        current_rel_path: str = "README.md",
        repo_root: Optional[Path] = None,
        link_map: Optional[Dict[str, SyncTarget]] = None,
        add_source_banner: bool = True,
    ) -> None:
        self.repo_name = repo_name
        self.space_key = space_key
        self.current_rel_path = current_rel_path.replace("\\", "/")
        self.repo_root = repo_root
        self.link_map = link_map or {}
        self.add_source_banner = add_source_banner
        self.attachments: List[Path] = []
        self._seen_attachments: set[Path] = set()

    def _resolve_repo_relative(self, href_path: str) -> str:
        """Resolve a relative link/image path against `self.current_rel_path`."""
        cur_dir = posixpath.dirname(self.current_rel_path)
        joined = posixpath.join(cur_dir, href_path) if cur_dir else href_path
        norm = posixpath.normpath(joined)
        if norm.startswith("./"):
            norm = norm[2:]
        return norm

    def _render_image(self, alt: str, src: str) -> str:
        src = src.strip()
        alt_attr = f' ac:alt="{_escape_xml(alt)}"' if alt else ""
        if src.startswith(("http://", "https://")):
            return f'<ac:image{alt_attr}><ri:url ri:value="{_escape_xml(src)}" /></ac:image>'

        # Local image -> record attachment path if repo_root is known and file exists
        clean_src = src.split("?", 1)[0].split("#", 1)[0]
        rel_img = self._resolve_repo_relative(clean_src)
        filename = posixpath.basename(rel_img)
        if self.repo_root is not None:
            candidate = (self.repo_root / rel_img).resolve()
            if candidate.exists() and candidate.is_file() and candidate not in self._seen_attachments:
                self._seen_attachments.add(candidate)
                self.attachments.append(candidate)

        return (
            f"<ac:image{alt_attr}>"
            f'<ri:attachment ri:filename="{_escape_xml(filename)}" />'
            "</ac:image>"
        )

    def _render_link(self, label: str, href: str) -> str:
        href = href.strip()
        if not href.startswith(("http://", "https://", "mailto:", "#")):
            # Check if this relative link points to a synced Markdown file in `link_map`
            path_part, _, anchor = href.partition("#")
            if path_part.lower().endswith((".md", ".markdown")):
                resolved_rel = self._resolve_repo_relative(path_part)
                target = self.link_map.get(resolved_rel)
                if target is not None:
                    space = _escape_xml(target.space_key or self.space_key)
                    title = _escape_xml(target.title)
                    anchor_attr = f' ac:anchor="{_escape_xml(anchor)}"' if anchor else ""
                    safe_label = _escape_cdata(label)
                    return (
                        f"<ac:link{anchor_attr}>"
                        f'<ri:page ri:space-key="{space}" ri:content-title="{title}" />'
                        f"<ac:plain-text-link-body><![CDATA[{safe_label}]]></ac:plain-text-link-body>"
                        "</ac:link>"
                    )

        return f'<a href="{_escape_xml(href)}">{self._render_inline(label, allow_links=False)}</a>'

    def _render_inline(self, text: str, *, allow_links: bool = True) -> str:
        """Render inline Markdown formatting (code spans, images, links, bold, italic, strikethrough)."""
        placeholders: List[str] = []

        def _stash(xhtml_fragment: str) -> str:
            idx = len(placeholders)
            placeholders.append(xhtml_fragment)
            return f"\x00PH{idx}\x00"

        # 1. Inline code spans `...`
        text = re.sub(
            r"`([^`]+)`",
            lambda m: _stash(f"<code>{_escape_xml(m.group(1))}</code>"),
            text,
        )

        # 2. Inline images ![alt](src)
        text = re.sub(
            r"!\[([^\]]*)\]\(([^)]+)\)",
            lambda m: _stash(self._render_image(m.group(1), m.group(2))),
            text,
        )

        # 3. Inline links [label](href)
        if allow_links:
            text = re.sub(
                r"\[([^\]]+)\]\(([^)]+)\)",
                lambda m: _stash(self._render_link(m.group(1), m.group(2))),
                text,
            )

        # 4. Escape XML on remaining plain text
        escaped = _escape_xml(text)

        # 5. Bold + Italic, Bold, Italic, Strikethrough
        escaped = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", escaped)
        escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"__(.+?)__", r"<strong>\1</strong>", escaped)
        escaped = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", escaped)
        escaped = re.sub(r"(?<!\w)_([^_\n]+)_(?!\w)", r"<em>\1</em>", escaped)
        escaped = re.sub(r"~~(.+?)~~", r"<del>\1</del>", escaped)

        # 6. Restore stashed fragments
        for idx, fragment in enumerate(placeholders):
            escaped = escaped.replace(f"\x00PH{idx}\x00", fragment)

        return escaped

    @staticmethod
    def _split_table_row(line: str) -> List[str]:
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [cell.strip() for cell in stripped.split("|")]

    def _render_table(self, header_line: str, body_lines: List[str]) -> str:
        headers = self._split_table_row(header_line)
        th_cells = "".join(f"<th>{self._render_inline(h)}</th>" for h in headers)
        thead = f"<thead><tr>{th_cells}</tr></thead>"

        rows_xhtml: List[str] = []
        for row_line in body_lines:
            cells = self._split_table_row(row_line)
            # Pad or trim to match header count
            if len(cells) < len(headers):
                cells.extend([""] * (len(headers) - len(cells)))
            td_cells = "".join(f"<td>{self._render_inline(c)}</td>" for c in cells[: len(headers)])
            rows_xhtml.append(f"<tr>{td_cells}</tr>")

        tbody = f"<tbody>{''.join(rows_xhtml)}</tbody>"
        return f"<table>{thead}{tbody}</table>"

    def _render_blockquote_or_alert(self, quote_lines: List[str]) -> str:
        if not quote_lines:
            return ""
        first_Match = _ALERT_HEADER_RE.match(quote_lines[0])
        if first_Match:
            alert_type = first_Match.group(1).upper()
            inline_remainder = first_Match.group(2).strip()
            macro_name = ALERT_MACRO_MAP.get(alert_type, "info")
            body_lines = ([inline_remainder] if inline_remainder else []) + quote_lines[1:]
            inner_md = "\n".join(body_lines).strip()
            inner_xhtml = self._render_blocks(inner_md) if inner_md else "<p></p>"
            return build_panel_macro(macro_name, inner_xhtml)

        inner_md = "\n".join(quote_lines).strip()
        inner_xhtml = self._render_blocks(inner_md)
        return f"<blockquote>{inner_xhtml}</blockquote>"

    def _render_list(self, items: List[Tuple[str, str]]) -> str:
        """Render a contiguous list of `('ul' | 'ol', item_text)` tuples."""
        if not items:
            return ""
        list_tag = items[0][0]
        li_fragments = [f"<li>{self._render_inline(text)}</li>" for _, text in items]
        return f"<{list_tag}>{''.join(li_fragments)}</{list_tag}>"

    def _render_blocks(self, markdown_text: str) -> str:
        lines = markdown_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        n = len(lines)
        i = 0
        blocks: List[str] = []

        while i < n:
            line = lines[i]

            # 1. Blank line
            if not line.strip():
                i += 1
                continue

            # 2. Fenced code block
            fence_match = _FENCE_RE.match(line)
            if fence_match:
                fence_marker = fence_match.group(1)
                fence_char = fence_marker[0]
                fence_len = len(fence_marker)
                lang = fence_match.group(2).strip() or None
                i += 1
                code_lines: List[str] = []
                while i < n:
                    close_check = lines[i].strip()
                    if close_check.startswith(fence_char * fence_len) and set(close_check) == {fence_char}:
                        i += 1
                        break
                    code_lines.append(lines[i])
                    i += 1
                blocks.append(build_code_macro("\n".join(code_lines), language=lang))
                continue

            # 3. TOC marker (`[TOC]` or `[[_TOC_]]`)
            if _TOC_RE.match(line):
                blocks.append('<ac:structured-macro ac:name="toc" />')
                i += 1
                continue

            # 4. Horizontal rule
            if _HR_RE.match(line):
                blocks.append("<hr />")
                i += 1
                continue

            # 5. ATX Heading (`# H1` .. `###### H6`)
            heading_match = _HEADING_RE.match(line)
            if heading_match:
                level = len(heading_match.group(1))
                title_text = heading_match.group(2).strip()
                blocks.append(f"<h{level}>{self._render_inline(title_text)}</h{level}>")
                i += 1
                continue

            # 6. GFM Pipe Table (header line followed by separator line `| --- | --- |`)
            if "|" in line and (i + 1) < n and _TABLE_SEP_RE.match(lines[i + 1]):
                header_line = line
                i += 2  # skip header + separator
                body_lines: List[str] = []
                while i < n and lines[i].strip() and "|" in lines[i]:
                    body_lines.append(lines[i])
                    i += 1
                blocks.append(self._render_table(header_line, body_lines))
                continue

            # 7. Blockquote or GitHub-flavored Alert (`> ...`)
            if line.lstrip().startswith(">"):
                quote_lines: List[str] = []
                while i < n and lines[i].lstrip().startswith(">"):
                    stripped_q = re.sub(r"^\s*>\s?", "", lines[i])
                    quote_lines.append(stripped_q)
                    i += 1
                blocks.append(self._render_blockquote_or_alert(quote_lines))
                continue

            # 8. Unordered or Ordered List
            ul_match = _UL_RE.match(line)
            ol_match = _OL_RE.match(line)
            if ul_match or ol_match:
                list_type = "ul" if ul_match else "ol"
                items: List[Tuple[str, str]] = []
                while i < n:
                    m_ul = _UL_RE.match(lines[i])
                    m_ol = _OL_RE.match(lines[i])
                    if list_type == "ul" and m_ul:
                        items.append(("ul", m_ul.group(2).strip()))
                        i += 1
                    elif list_type == "ol" and m_ol:
                        items.append(("ol", m_ol.group(2).strip()))
                        i += 1
                    else:
                        break
                blocks.append(self._render_list(items))
                continue

            # 9. Paragraph (collect contiguous non-special lines)
            para_lines: List[str] = []
            while i < n and lines[i].strip():
                if (
                    _FENCE_RE.match(lines[i])
                    or _TOC_RE.match(lines[i])
                    or _HR_RE.match(lines[i])
                    or _HEADING_RE.match(lines[i])
                    or lines[i].lstrip().startswith(">")
                    or _UL_RE.match(lines[i])
                    or _OL_RE.match(lines[i])
                    or ("|" in lines[i] and (i + 1) < n and _TABLE_SEP_RE.match(lines[i + 1]))
                ):
                    break
                para_lines.append(lines[i].strip())
                i += 1
            if para_lines:
                joined_para = " ".join(para_lines)
                blocks.append(f"<p>{self._render_inline(joined_para)}</p>")

        return "\n".join(blocks)

    def convert(self, markdown_text: str) -> ConversionResult:
        """Convert Markdown text into Confluence Storage Format (XHTML)."""
        body_xhtml = self._render_blocks(markdown_text)
        parts: List[str] = []
        if self.add_source_banner:
            parts.append(build_source_banner(self.repo_name, self.current_rel_path))
        if body_xhtml:
            parts.append(body_xhtml)
        return ConversionResult(xhtml="\n".join(parts), attachments=list(self.attachments))


def convert_markdown_to_storage(
    markdown_text: str,
    *,
    repo_name: str = "",
    space_key: str = "ENG",
    current_rel_path: str = "README.md",
    repo_root: Optional[Path] = None,
    link_map: Optional[Dict[str, SyncTarget]] = None,
    add_source_banner: bool = True,
) -> ConversionResult:
    """Convenience function to convert Markdown text to Confluence Storage Format XHTML."""
    converter = ConfluenceConverter(
        repo_name=repo_name,
        space_key=space_key,
        current_rel_path=current_rel_path,
        repo_root=repo_root,
        link_map=link_map,
        add_source_banner=add_source_banner,
    )
    return converter.convert(markdown_text)
