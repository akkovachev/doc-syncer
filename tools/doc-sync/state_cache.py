"""Conflict Resolver and State Cache (`/tools/doc-sync/state_cache.py`).

Implements Section 3.3 & Section 3.6 of the Architecture Proposal:
- Computes a deterministic SHA-256 hash of each file's rendered Confluence XHTML payload.
- Reads and persists the JSON state cache (`.doc-sync-cache.json`, cached across runs via `actions/cache`).
- Skips unchanged files before network I/O when the local SHA-256 matches the cached hash.
- Detects direct Confluence edits (optimistic-lock conflict) when the remote Confluence page
  `version.number` is greater than the `version` last written by the engine (`cached.version`),
  unless `force_overwrite: true` is configured for that mapping.
- Generates unified diffs between current Confluence storage XHTML and local rendered XHTML.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Union


def compute_xhtml_hash(xhtml: str) -> str:
    """Compute a normalized SHA-256 hex digest of rendered Confluence XHTML."""
    normalized = xhtml.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def compute_unified_diff(
    remote_xhtml: str,
    local_xhtml: str,
    *,
    from_label: str = "confluence/current",
    to_label: str = "local/rendered",
    context_lines: int = 3,
) -> str:
    """Produce a readable unified diff between current Confluence XHTML and local rendered XHTML."""
    remote_lines = remote_xhtml.replace("\r\n", "\n").strip().splitlines(keepends=True)
    local_lines = local_xhtml.replace("\r\n", "\n").strip().splitlines(keepends=True)
    diff_iter = difflib.unified_diff(
        remote_lines,
        local_lines,
        fromfile=from_label,
        tofile=to_label,
        n=context_lines,
    )
    return "".join(diff_iter).strip()


@dataclass
class PageCacheEntry:
    """State cache record for a single synced Confluence page."""

    page_id: str
    source_rel_path: str
    content_hash: str
    version: int
    title: str = ""
    updated_at: str = ""


@dataclass
class ConflictEvaluation:
    """Result of comparing local rendered XHTML + state cache against Confluence page state."""

    decision: str  # "skip_unchanged" | "update" | "conflict"
    reason: str
    local_hash: str
    cached_version: Optional[int] = None
    remote_version: Optional[int] = None
    diff: str = ""


@dataclass
class StateCache:
    """Persistent JSON state cache mapping `source_rel_path` (and `page_id`) to `PageCacheEntry`."""

    cache_path: Path
    entries: Dict[str, PageCacheEntry] = field(default_factory=dict)

    @classmethod
    def load(cls, cache_path: Union[str, Path]) -> "StateCache":
        path = Path(cache_path)
        if not path.exists():
            return cls(cache_path=path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            pages_raw = raw.get("pages", {}) if isinstance(raw, dict) else {}
            entries: Dict[str, PageCacheEntry] = {}
            for key, item in pages_raw.items():
                if isinstance(item, dict) and "page_id" in item and "content_hash" in item:
                    entries[key] = PageCacheEntry(
                        page_id=str(item["page_id"]),
                        source_rel_path=str(item.get("source_rel_path", key)),
                        content_hash=str(item["content_hash"]),
                        version=int(item.get("version", 1)),
                        title=str(item.get("title", "")),
                        updated_at=str(item.get("updated_at", "")),
                    )
            return cls(cache_path=path, entries=entries)
        except Exception:
            # Corrupted cache falls back cleanly to empty state rather than crashing the run
            return cls(cache_path=path)

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "pages": {k: asdict(v) for k, v in sorted(self.entries.items())},
        }
        self.cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def get(self, source_rel_path: str, page_id: Optional[str] = None) -> Optional[PageCacheEntry]:
        norm = source_rel_path.replace("\\", "/")
        if norm in self.entries:
            return self.entries[norm]
        if page_id:
            for entry in self.entries.values():
                if entry.page_id == str(page_id):
                    return entry
        return None

    def is_unchanged_locally(self, source_rel_path: str, local_xhtml: str, page_id: Optional[str] = None) -> bool:
        """Fast pre-network check: returns True if rendered XHTML matches cached SHA-256 hash."""
        entry = self.get(source_rel_path, page_id=page_id)
        if entry is None:
            return False
        return entry.content_hash == compute_xhtml_hash(local_xhtml)

    def evaluate_remote_state(
        self,
        *,
        source_rel_path: str,
        page_id: str,
        local_xhtml: str,
        remote_version: int,
        remote_xhtml: str,
        force_overwrite: bool = False,
    ) -> ConflictEvaluation:
        """Compare local XHTML + cached version against remote Confluence page version and body."""
        local_hash = compute_xhtml_hash(local_xhtml)
        remote_hash = compute_xhtml_hash(remote_xhtml)
        entry = self.get(source_rel_path, page_id=page_id)
        cached_version = entry.version if entry is not None else None

        diff_text = compute_unified_diff(
            remote_xhtml,
            local_xhtml,
            from_label=f"confluence/page-{page_id}@v{remote_version}",
            to_label=f"repo/{source_rel_path}",
        )

        # Case 1: Remote version exceeds the version we last wrote -> someone edited Confluence directly
        if cached_version is not None and remote_version > cached_version:
            if not force_overwrite:
                return ConflictEvaluation(
                    decision="conflict",
                    reason=(
                        f"Confluence page {page_id} was edited directly "
                        f"(remote version {remote_version} > last synced version {cached_version}) "
                        "— manual merge required"
                    ),
                    local_hash=local_hash,
                    cached_version=cached_version,
                    remote_version=remote_version,
                    diff=diff_text,
                )
            return ConflictEvaluation(
                decision="update",
                reason=(
                    f"Remote version {remote_version} > cached version {cached_version}, "
                    "but force_overwrite=true is set for this mapping"
                ),
                local_hash=local_hash,
                cached_version=cached_version,
                remote_version=remote_version,
                diff=diff_text,
            )

        # Case 2: Remote content is already identical to our rendered XHTML
        if remote_hash == local_hash:
            return ConflictEvaluation(
                decision="skip_unchanged",
                reason=f"Confluence page {page_id} (v{remote_version}) already matches local XHTML hash",
                local_hash=local_hash,
                cached_version=cached_version,
                remote_version=remote_version,
                diff="",
            )

        # Case 3: Safe to update
        return ConflictEvaluation(
            decision="update",
            reason=f"Local changes detected for {source_rel_path}; updating page {page_id} to v{remote_version + 1}",
            local_hash=local_hash,
            cached_version=cached_version,
            remote_version=remote_version,
            diff=diff_text,
        )

    def record_sync(
        self,
        *,
        source_rel_path: str,
        page_id: str,
        content_hash: str,
        version: int,
        title: str = "",
    ) -> PageCacheEntry:
        """Update the state cache after a successful publish (or verified identical state)."""
        norm = source_rel_path.replace("\\", "/")
        entry = PageCacheEntry(
            page_id=str(page_id),
            source_rel_path=norm,
            content_hash=content_hash,
            version=int(version),
            title=title,
            updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        self.entries[norm] = entry
        return entry
