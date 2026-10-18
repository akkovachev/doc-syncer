# Consumer Repository Onboarding Guide

> [!TIP]
> You can generate a starter `.doc-sync.yml` automatically by running `tools doc-sync init` in the root of your repository.

## 1. Add `.doc-sync.yml` to Your Repository Root

Create `.doc-sync.yml` at the root of your repository pointing your Markdown files at Confluence page IDs (see [Internal Tooling Suite — Overview](../README.md) and [Changelog](../CHANGELOG.md)).

```yaml
version: 1

confluence:
  space_key: ENG
  base_url_secret: CONFLUENCE_BASE_URL
  auth_secret: CONFLUENCE_PAT

mappings:
  - source: README.md
    confluence_page_id: "123456789"
    title: "My Service — Overview"
```

## 2. Add the 3-Line Caller Workflow

Create `.github/workflows/doc-sync.yml` in the consumer repository:

```yaml
name: Doc Sync
on:
  push:
    branches: [main]
    paths: ["**/*.md", ".doc-sync.yml"]
jobs:
  sync:
    uses: org/internal-tools/.github/workflows/doc-sync.yml@v1
    secrets: inherit
```
