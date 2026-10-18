"""Authentication, GitHub OIDC, and secret indirection helpers (`/tools/common/auth.py`).

Implements Section 3.5 & Section 4 of the Architecture Proposal:
- `.doc-sync.yml` stores secret *names* (e.g. `CONFLUENCE_BASE_URL`, `CONFLUENCE_PAT`),
  never raw credential values.
- At runtime, secrets are resolved from environment variables injected by GitHub Actions
  and immediately masked via `::add-mask::`.
- Supports Confluence Personal Access Tokens (PAT / Bearer), Basic Auth (Cloud email + API token),
  OAuth2 3LO client credentials exchange, and GitHub Actions OIDC token requests.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Dict, Optional

import requests

from .logging import ActionsLogger, get_logger


class SecretResolutionError(RuntimeError):
    """Raised when a required secret environment variable is missing or invalid."""


@dataclass(frozen=True)
class ConfluenceCredentials:
    """Resolved runtime credentials for talking to Confluence REST API."""

    base_url: str
    auth_token: str
    auth_mode: str = "bearer"  # "bearer" (PAT / Data Center / OAuth2) or "basic" (Cloud email:token)
    user_email: Optional[str] = None
    oidc_token: Optional[str] = None

    def build_auth_headers(self) -> Dict[str, str]:
        """Return HTTP Authorization header dictionary."""
        if self.auth_mode == "basic" and self.user_email:
            raw = f"{self.user_email}:{self.auth_token}".encode("utf-8")
            encoded = base64.b64encode(raw).decode("ascii")
            return {"Authorization": f"Basic {encoded}"}
        # If auth_token already starts with "Basic " or "Bearer ", preserve it
        if self.auth_token.startswith(("Bearer ", "Basic ")):
            return {"Authorization": self.auth_token}
        # Support "email:api_token" combined format in CONFLUENCE_PAT for Confluence Cloud convenience
        if ":" in self.auth_token and "@" in self.auth_token.split(":", 1)[0]:
            encoded = base64.b64encode(self.auth_token.encode("utf-8")).decode("ascii")
            return {"Authorization": f"Basic {encoded}"}
        return {"Authorization": f"Bearer {self.auth_token}"}


def resolve_secret(
    secret_name: str,
    *,
    required: bool = True,
    mask: bool = True,
    env: Optional[Dict[str, str]] = None,
    logger: Optional[ActionsLogger] = None,
) -> Optional[str]:
    """Resolve a secret value from environment variables by its configured name."""
    log = logger or get_logger("auth")
    source_env = env if env is not None else os.environ
    value = source_env.get(secret_name, "").strip()
    if not value:
        if required:
            raise SecretResolutionError(
                f"Required secret '{secret_name}' is not set in the environment. "
                f"Ensure it is injected via GitHub Actions org/environment secrets."
            )
        return None
    if mask:
        log.mask_secret(value)
    return value


def request_github_oidc_token(
    audience: str = "confluence-doc-sync",
    *,
    timeout: float = 10.0,
    logger: Optional[ActionsLogger] = None,
) -> Optional[str]:
    """Request a GitHub Actions OIDC JWT if running inside an OIDC-enabled workflow job."""
    log = logger or get_logger("auth")
    req_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    req_token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not req_url or not req_token:
        log.debug("GitHub OIDC environment variables not present; skipping OIDC token request.")
        return None

    sep = "&" if "?" in req_url else "?"
    url = f"{req_url}{sep}audience={audience}"
    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {req_token}", "Accept": "application/json; api-version=2.0"},
            timeout=timeout,
        )
        resp.raise_for_status()
        jwt_val = resp.json().get("value")
        if jwt_val:
            log.mask_secret(jwt_val)
            log.debug("Successfully obtained GitHub Actions OIDC token.")
        return jwt_val
    except Exception as exc:
        log.warning(f"Failed to fetch GitHub Actions OIDC token: {exc}", title="OIDC Warning")
        return None


def resolve_confluence_credentials(
    confluence_cfg: Dict[str, object],
    *,
    env: Optional[Dict[str, str]] = None,
    logger: Optional[ActionsLogger] = None,
) -> ConfluenceCredentials:
    """Resolve `base_url_secret` and `auth_secret` from the `.doc-sync.yml` `confluence` block."""
    log = logger or get_logger("auth")
    base_url_secret = str(confluence_cfg.get("base_url_secret", "CONFLUENCE_BASE_URL"))
    auth_secret = str(confluence_cfg.get("auth_secret", "CONFLUENCE_PAT"))
    user_secret = confluence_cfg.get("username_secret")

    base_url = resolve_secret(base_url_secret, required=True, mask=False, env=env, logger=log)
    auth_token = resolve_secret(auth_secret, required=True, mask=True, env=env, logger=log)
    user_email = (
        resolve_secret(str(user_secret), required=False, mask=False, env=env, logger=log)
        if user_secret
        else (env if env is not None else os.environ).get("CONFLUENCE_USER_EMAIL", "").strip() or None
    )

    assert base_url is not None and auth_token is not None
    oidc_token = request_github_oidc_token(logger=log)
    auth_mode = "basic" if user_email else "bearer"

    return ConfluenceCredentials(
        base_url=base_url.rstrip("/"),
        auth_token=auth_token,
        auth_mode=auth_mode,
        user_email=user_email,
        oidc_token=oidc_token,
    )
