"""Shared HTTP client with exponential backoff + jitter (`/tools/common/http.py`).

Implements Section 3.6 & Section 4 of the Architecture Proposal:
- All HTTP calls use exponential backoff with jitter on HTTP 429 and 5xx (500, 502, 503, 504)
  and transient connection/timeout errors, capped at 5 attempts by default.
- Exposes `HttpError` carrying status code and response payload so callers (such as
  `confluence_client.py` handling HTTP 409 version conflicts) can inspect and react cleanly.
- Includes a Slack webhook notification helper reusing the same resilient client.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Set

import requests

from .logging import ActionsLogger, get_logger

RETRYABLE_STATUS_CODES: Set[int] = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    """Raised when an HTTP request returns a non-2xx status code after retries."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        url: Optional[str] = None,
        response_text: Optional[str] = None,
        response_json: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.url = url
        self.response_text = response_text
        self.response_json = response_json


@dataclass
class HttpClient:
    """Requests session wrapper with exponential backoff + full jitter."""

    base_headers: Dict[str, str] = field(default_factory=dict)
    max_attempts: int = 5
    base_delay: float = 0.5
    max_delay: float = 16.0
    timeout: float = 30.0
    sleep_fn: Callable[[float], None] = time.sleep
    session: Optional[requests.Session] = None
    logger: Optional[ActionsLogger] = None

    def __post_init__(self) -> None:
        if self.session is None:
            self.session = requests.Session()
        if self.logger is None:
            self.logger = get_logger("http")
        if self.base_headers:
            self.session.headers.update(self.base_headers)

    def _compute_backoff(self, attempt: int, retry_after: Optional[str] = None) -> float:
        """Compute exponential backoff with jitter (1-indexed attempt)."""
        if retry_after:
            try:
                val = float(retry_after)
                if val >= 0:
                    return min(val, self.max_delay)
            except ValueError:
                pass
        cap = min(self.max_delay, self.base_delay * (2 ** (attempt - 1)))
        # Full jitter between 50% and 100% of cap for deterministic bounds + spread
        return random.uniform(cap * 0.5, cap)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        data: Optional[Any] = None,
        files: Optional[Any] = None,
        timeout: Optional[float] = None,
    ) -> requests.Response:
        """Execute an HTTP request with retry + exponential backoff on 429/5xx."""
        assert self.session is not None and self.logger is not None
        req_timeout = timeout if timeout is not None else self.timeout
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.session.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_body,
                    data=data,
                    files=files,
                    timeout=req_timeout,
                )
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_attempts:
                    raise HttpError(
                        f"HTTP {method.upper()} {url} failed after {attempt} attempts: {exc}",
                        url=url,
                    ) from exc
                delay = self._compute_backoff(attempt)
                self.logger.warning(
                    f"Transient network error on {method.upper()} {url} "
                    f"(attempt {attempt}/{self.max_attempts}): {exc}. Retrying in {delay:.2f}s..."
                )
                self.sleep_fn(delay)
                continue

            if response.status_code in RETRYABLE_STATUS_CODES:
                if attempt < self.max_attempts:
                    retry_after = response.headers.get("Retry-After")
                    delay = self._compute_backoff(attempt, retry_after=retry_after)
                    self.logger.warning(
                        f"HTTP {response.status_code} on {method.upper()} {url} "
                        f"(attempt {attempt}/{self.max_attempts}). Retrying in {delay:.2f}s..."
                    )
                    self.sleep_fn(delay)
                    continue

            if response.status_code >= 400:
                parsed_json = None
                try:
                    parsed_json = response.json()
                except Exception:
                    pass
                raise HttpError(
                    f"HTTP {response.status_code} error for {method.upper()} {url}: "
                    f"{response.text[:500]}",
                    status_code=response.status_code,
                    url=url,
                    response_text=response.text,
                    response_json=parsed_json,
                )

            return response

        raise HttpError(
            f"HTTP {method.upper()} {url} exhausted {self.max_attempts} attempts: {last_exc}",
            url=url,
        )

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", url, **kwargs)

    def post_slack_webhook(self, webhook_url: str, text: str, blocks: Optional[list] = None) -> bool:
        """Send an alert payload to a Slack Incoming Webhook using the shared HTTP client."""
        assert self.logger is not None
        payload: Dict[str, Any] = {"text": text}
        if blocks:
            payload["blocks"] = blocks
        try:
            self.post(webhook_url, json_body=payload)
            return True
        except HttpError as exc:
            self.logger.warning(f"Failed to send Slack webhook notification: {exc}", title="Slack Notification")
            return False
