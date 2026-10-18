"""Confluence REST API Client (`/tools/doc-sync/confluence_client.py`).

Implements Section 3.5 & Section 3.6 of the Architecture Proposal:
- Reads page state via `GET /wiki/rest/api/content/{pageId}?expand=version,body.storage,space`
- Publishes updates via `PUT /wiki/rest/api/content/{pageId}` with optimistic locking
  (`version.number = current_version + 1`).
- Handles HTTP 409 (race-condition version conflict between GET and PUT):
  re-fetches the page once and retries with the fresh version (when allowed by conflict rules)
  before escalating to `ConfluenceConflictError`.
- Supports child page lookup/creation under `confluence_parent_page_id` (for glob mappings),
  Confluence label synchronization, and image attachment uploads.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from tools.common.auth import ConfluenceCredentials
from tools.common.http import HttpClient, HttpError
from tools.common.logging import ActionsLogger, get_logger


class ConfluenceConflictError(RuntimeError):
    """Raised when a PUT fails with a version conflict that cannot be safely auto-retried."""

    def __init__(self, message: str, *, page_id: str, remote_version: Optional[int] = None) -> None:
        super().__init__(message)
        self.page_id = page_id
        self.remote_version = remote_version


@dataclass
class ConfluencePage:
    """Representation of a Confluence page returned by the REST API."""

    page_id: str
    title: str
    space_key: str
    version: int
    body_storage: str
    web_url: str


class ConfluenceClient:
    """Client for Confluence Cloud and Data Center REST API (`/rest/api/content`)."""

    def __init__(
        self,
        credentials: ConfluenceCredentials,
        *,
        http_client: Optional[HttpClient] = None,
        logger: Optional[ActionsLogger] = None,
    ) -> None:
        self.credentials = credentials
        self.logger = logger or get_logger("confluence")
        headers = {
            "Accept": "application/json",
            **credentials.build_auth_headers(),
        }
        self.http = http_client or HttpClient(base_headers=headers, logger=self.logger)
        # Ensure auth headers are applied even if a custom HttpClient was injected
        if self.http.session is not None:
            self.http.session.headers.update(headers)

    @property
    def api_base(self) -> str:
        """Normalize base URL to include `/wiki/rest/api` (Cloud) or `/rest/api` if already rooted."""
        base = self.credentials.base_url.rstrip("/")
        if base.endswith("/rest/api"):
            return base
        if base.endswith("/wiki"):
            return f"{base}/rest/api"
        return f"{base}/wiki/rest/api"

    def build_page_web_url(self, page_id: str, space_key: str = "") -> str:
        base = self.credentials.base_url.rstrip("/")
        if not base.endswith("/wiki") and "/wiki/" not in base:
            base = f"{base}/wiki"
        if space_key:
            return f"{base}/spaces/{space_key}/pages/{page_id}"
        return f"{base}/pages/viewpage.action?pageId={page_id}"

    def _parse_page_payload(self, data: Dict[str, Any], fallback_space: str = "") -> ConfluencePage:
        page_id = str(data["id"])
        title = str(data.get("title", ""))
        space_key = str((data.get("space") or {}).get("key") or fallback_space)
        version_num = int((data.get("version") or {}).get("number", 1))
        body_val = str(((data.get("body") or {}).get("storage") or {}).get("value", ""))

        links = data.get("_links") or {}
        webui = links.get("webui", "")
        base_link = links.get("base") or self.credentials.base_url.rstrip("/")
        web_url = f"{base_link.rstrip('/')}{webui}" if webui else self.build_page_web_url(page_id, space_key)

        return ConfluencePage(
            page_id=page_id,
            title=title,
            space_key=space_key,
            version=version_num,
            body_storage=body_val,
            web_url=web_url,
        )

    def get_page(self, page_id: str) -> ConfluencePage:
        """Fetch current page metadata, version, and `body.storage` via GET."""
        url = f"{self.api_base}/content/{page_id}"
        resp = self.http.get(url, params={"expand": "version,body.storage,space"})
        return self._parse_page_payload(resp.json())

    def find_child_page_by_title(self, space_key: str, parent_page_id: str, title: str) -> Optional[ConfluencePage]:
        """Look up an existing child page under `parent_page_id` matching `title`."""
        url = f"{self.api_base}/content"
        resp = self.http.get(
            url,
            params={
                "spaceKey": space_key,
                "title": title,
                "type": "page",
                "expand": "version,body.storage,space,ancestors",
            },
        )
        results = (resp.json() or {}).get("results") or []
        for item in results:
            ancestors = item.get("ancestors") or []
            if not ancestors or any(str(a.get("id")) == str(parent_page_id) for a in ancestors):
                return self._parse_page_payload(item, fallback_space=space_key)
        return None

    def create_child_page(
        self,
        *,
        space_key: str,
        parent_page_id: str,
        title: str,
        body_xhtml: str,
    ) -> ConfluencePage:
        """Create a new Confluence page under `parent_page_id` (used for glob mappings)."""
        url = f"{self.api_base}/content"
        payload = {
            "type": "page",
            "title": title,
            "space": {"key": space_key},
            "ancestors": [{"id": str(parent_page_id)}],
            "body": {
                "storage": {
                    "value": body_xhtml,
                    "representation": "storage",
                }
            },
        }
        resp = self.http.post(
            url,
            headers={"Content-Type": "application/json"},
            json_body=payload,
        )
        return self._parse_page_payload(resp.json(), fallback_space=space_key)

    def update_page(
        self,
        *,
        page_id: str,
        title: str,
        space_key: str,
        body_xhtml: str,
        current_version: int,
        allow_retry_on_409: bool = True,
        conflict_check_fn: Optional[Callable[[ConfluencePage], bool]] = None,
    ) -> ConfluencePage:
        """Publish updated Storage Format XHTML to Confluence with optimistic locking.

        Per Section 3.6:
        - Sends `PUT /wiki/rest/api/content/{pageId}` with `version.number = current_version + 1`.
        - If Confluence rejects the PUT with HTTP 409 (version conflict), re-fetches the page once
          and retries once with the fresh version (provided `conflict_check_fn` allows it) before
          escalating to `ConfluenceConflictError`.
        """
        url = f"{self.api_base}/content/{page_id}"

        def _build_put_payload(next_version: int) -> Dict[str, Any]:
            return {
                "id": str(page_id),
                "type": "page",
                "title": title,
                "space": {"key": space_key},
                "version": {
                    "number": next_version,
                    "minorEdit": False,
                    "message": "Synced automatically by Internal Tooling Suite (doc-sync)",
                },
                "body": {
                    "storage": {
                        "value": body_xhtml,
                        "representation": "storage",
                    }
                },
            }

        try:
            resp = self.http.put(
                url,
                headers={"Content-Type": "application/json"},
                json_body=_build_put_payload(current_version + 1),
            )
            return self._parse_page_payload(resp.json(), fallback_space=space_key)
        except HttpError as exc:
            if exc.status_code != 409 or not allow_retry_on_409:
                if exc.status_code == 409:
                    raise ConfluenceConflictError(
                        f"HTTP 409 version conflict updating Confluence page {page_id}.",
                        page_id=page_id,
                    ) from exc
                raise

            # Section 3.6: Re-fetch once on 409 and retry once if conflict rules allow
            self.logger.warning(
                f"Received HTTP 409 on PUT for page {page_id} (expected v{current_version + 1}). "
                "Re-fetching fresh version for single retry..."
            )
            fresh_page = self.get_page(page_id)
            if conflict_check_fn is not None and not conflict_check_fn(fresh_page):
                raise ConfluenceConflictError(
                    f"Confluence page {page_id} was modified concurrently to v{fresh_page.version} "
                    "and cannot be overwritten safely — manual merge required",
                    page_id=page_id,
                    remote_version=fresh_page.version,
                ) from exc

            try:
                retry_resp = self.http.put(
                    url,
                    headers={"Content-Type": "application/json"},
                    json_body=_build_put_payload(fresh_page.version + 1),
                )
                return self._parse_page_payload(retry_resp.json(), fallback_space=space_key)
            except HttpError as retry_exc:
                if retry_exc.status_code == 409:
                    raise ConfluenceConflictError(
                        f"Confluence page {page_id} rejected retry PUT with second HTTP 409 "
                        f"(at v{fresh_page.version + 1}) — manual merge required",
                        page_id=page_id,
                        remote_version=fresh_page.version,
                    ) from retry_exc
                raise

    def sync_labels(self, page_id: str, labels: List[str]) -> None:
        """Apply configured Confluence labels to `page_id`."""
        if not labels:
            return
        url = f"{self.api_base}/content/{page_id}/label"
        payload = [{"prefix": "global", "name": lbl.strip().lower()} for lbl in labels if lbl.strip()]
        if not payload:
            return
        self.http.post(
            url,
            headers={"Content-Type": "application/json"},
            json_body=payload,
        )

    def upload_attachment(self, page_id: str, file_path: Path) -> None:
        """Upload or update a local image/asset attachment on `page_id`."""
        url = f"{self.api_base}/content/{page_id}/child/attachment"
        mime_type, _ = mimetypes.guess_type(str(file_path))
        content_type = mime_type or "application/octet-stream"
        with file_path.open("rb") as fh:
            self.http.post(
                url,
                headers={"X-Atlassian-Token": "nocheck"},
                params={"allowDuplicated": "true"},
                files={"file": (file_path.name, fh, content_type)},
            )
