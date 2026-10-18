"""Shared cross-cutting library (`/tools/common`) for the Internal Tooling Suite.

Provides authentication/secret resolution, YAML config loading & schema validation,
resilient HTTP client with exponential backoff, structured logging with GitHub Actions
annotations, and the plugin registration contract.
"""

from .auth import ConfluenceCredentials, SecretResolutionError, resolve_confluence_credentials, resolve_secret
from .config import ConfigValidationError, load_and_validate_config, load_yaml_file, validate_against_schema
from .http import HttpClient, HttpError
from .logging import ActionsLogger, get_logger

__all__ = [
    "ActionsLogger",
    "ConfigValidationError",
    "ConfluenceCredentials",
    "HttpClient",
    "HttpError",
    "SecretResolutionError",
    "get_logger",
    "load_and_validate_config",
    "load_yaml_file",
    "resolve_confluence_credentials",
    "resolve_secret",
    "validate_against_schema",
]
