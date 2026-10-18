# Internal Tooling Suite — Module 01: Doc-Sync Engine

[[_TOC_]]

> [!NOTE]
> This centralized monorepo hosts the **Internal Tooling Suite** (`tools` CLI) and its first production module, the **Doc-Sync Engine** (`tools/doc-sync`), which automatically synchronizes repository Markdown documentation to Confluence Storage Format (XHTML).

## Architecture Overview

See the interactive SVG architecture diagram in [`Doc-Sync-Architecture-Diagram.html`](./Doc-Sync-Architecture-Diagram.html), the [Onboarding Guide](./docs/onboarding-guide.md), and the [Release Changelog](./CHANGELOG.md).

| Component | Module Path | Responsibility |
| :--- | :--- | :--- |
| **Shared Plumbing** | `tools/common/` | Secret/OIDC resolution, YAML + JSON Schema validation, retry/backoff HTTP client, GH Actions logging, plugin registry |
| **Discovery Engine** | `tools/doc-sync/discovery.py` | Walks repo tree, validates `.doc-sync.yml`, expands globs, builds `md -> pageId` map |
| **Conflict Resolver** | `tools/doc-sync/state_cache.py` | SHA-256 XHTML hash cache + Confluence `version.number` optimistic-lock conflict detection |
| **XHTML Converter** | `tools/doc-sync/converter.py` | Converts CommonMark/GFM to Confluence Storage Format (`code`, `info`/`warning`, `toc`, tables, links, attachments) |
| **Confluence Client** | `tools/doc-sync/confluence_client.py` | REST API client (`GET`/`PUT /wiki/rest/api/content/{id}`) with single-retry on HTTP `409` |
| **Agentic Layer** | `tools/doc-sync/agent.py` | Advisory PR doc-completeness gate, `CHANGELOG.md` drafter, pre-sync diff summary, and `init` config generator |
| **Xray Scan (Module 02)** | `tools/xray-scan/` | Pluggable JFrog Xray security scan subcommand (Phase 4 roadmap) |

## Quick Start

```bash
# 1. Validate .doc-sync.yml against the JSON Schema
python bin/tools doc-sync validate-config --config .doc-sync.yml

# 2. Run a dry-run sync and inspect rendered Confluence Storage Format (.xhtml)
python bin/tools doc-sync dry-run --config .doc-sync.yml --output-dir ./dist/xhtml

# 3. Run the PR-time documentation completeness gate
python bin/tools doc-sync pr-check --changed-files tools/doc-sync/cli.py README.md --commits "feat(doc-sync): add 409 retry"

# 4. Generate a starter .doc-sync.yml for a new repository
python bin/tools doc-sync init --space-key ENG --output .doc-sync.starter.yml
```

> [!WARNING]
> Never commit raw Confluence PATs or webhook URLs into `.doc-sync.yml`. Always reference organization secret **names** (`CONFLUENCE_BASE_URL`, `CONFLUENCE_PAT`).
