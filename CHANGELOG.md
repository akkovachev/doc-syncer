# Changelog

All notable changes to the **Internal Tooling Suite** are documented in this file and synced to Confluence.

## [1.0.0] - 2026-10-18

### Features (`feat`)
- **common**: Add centralized plugin registry (`tools/common/plugin.py`), secret/OIDC resolver (`auth.py`), JSON Schema validator (`config.py`), structured GitHub Actions logger (`logging.py`), and exponential backoff HTTP client (`http.py`).
- **doc-sync**: Implement Discovery Engine (`discovery.py`), Markdown-to-Confluence Storage Format XHTML Converter (`converter.py`), SHA-256 & version-aware Conflict Resolver (`state_cache.py`), and Confluence REST API Client (`confluence_client.py`).
- **agent**: Add advisory PR-time documentation completeness gate (`pr-check`), pre-sync XHTML diff reviewer, and `.doc-sync.yml` onboarding generator (`init`).
- **workflows**: Add reusable GitHub Actions workflow (`.github/workflows/doc-sync.yml`) and composite action (`.github/actions/doc-sync/action.yml`).
