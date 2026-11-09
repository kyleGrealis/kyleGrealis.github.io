---
name: update-cv
description: >-
  Audit, enrich, and rebuild Kyle Grealis's University of Miami Curriculum Vitae
  (.docx and .pdf), sync the outputs to kylegrealis.com (static/files/ and
  docs/static/files/), and archive copies in ~/Documents/outputs/ (brain-wiki).
  Use when asked to update the CV, check for new publications/packages/talks,
  or sync CV files.
user-invocable: true
disable-model-invocation: false
---

# University of Miami CV Updater (`/update-cv`)

## Overview

Maintains Kyle Grealis's 27-section University of Miami Curriculum Vitae (`KGrealis_cv.docx` and `KGrealis_cv.pdf`). Queries PubMed, ORCID (`0000-0002-9223-8854`), OpenAlex, CRAN DOIs, and R/Medicine conference schedules against the canonical JSON source (`references/cv_data.json`), pauses for user confirmation before adding newly discovered works, rebuilds the `.docx` and `.pdf` via `references/um_cv_template.docx`, and syncs the updated files across `~/dev/kyleGrealis.github.io`, `~/Downloads`, and `~/Documents/outputs/` (`brain-wiki`).

## Dependencies

- **`pubmed-database`**: Use for deep PubMed metadata or abstract verification when a newly discovered PMID needs disambiguation.
- **`literature-search-openalex`**: Use for OpenAlex author/institution disambiguation beyond the automated ORCID query.
- **`commit-message`**: Format the git commit message when committing and pushing `kyleGrealis.github.io`.

## Quick Start

```bash
# 1. Discover any new publications, CRAN packages, or R/Medicine talks
uv run ~/dev/kyleGrealis.github.io/.agents/skills/update-cv/scripts/cv_tool.py discover \
  --limit 50 \
  --output /tmp/cv_discover.json

# 2. After confirming updates and editing references/cv_data.json, rebuild DOCX & PDF
uv run ~/dev/kyleGrealis.github.io/.agents/skills/update-cv/scripts/cv_tool.py build \
  --output /tmp/cv_build.json

# 3. Sync to website (static/files/ & docs/static/files/), ~/Downloads, and ~/Documents/outputs/
uv run ~/dev/kyleGrealis.github.io/.agents/skills/update-cv/scripts/cv_tool.py sync \
  --output /tmp/cv_sync.json
```

## Utility Scripts

### `scripts/cv_tool.py`

1. **`discover --limit <N> --output <path.json>`**
   - Queries OpenAlex (by ORCID `0000-0002-9223-8854`), PubMed E-utilities (`Grealis K[Author]`), and R/Medicine conference schedules.
   - Compares results against `references/cv_data.json` and writes any unrecorded items to `--output`.
   - **Gated Confirmation**: Always surface `new_works` to Kyle and wait for confirmation before adding entries to `references/cv_data.json`.
2. **`build [--data <path>] [--template <path>] --output <path.json>`**
   - Injects all 27 UM sections from `references/cv_data.json` into `references/um_cv_template.docx` to produce `static/files/KGrealis_cv.docx`.
   - Converts `static/files/KGrealis_cv.docx` to `static/files/KGrealis_cv.pdf` using Microsoft Word via `osascript`.
3. **`sync --output <path.json>`**
   - Copies `KGrealis_cv.pdf` to `docs/static/files/KGrealis_cv.pdf` and updates the `?v=YYYY-MM-DD` cache-buster on `index.qmd` and `docs/index.html`.
   - Copies dated and canonical `.docx`/`.pdf` files to `~/Downloads/`.
   - Copies `KGrealis_cv.docx` and `KGrealis_cv.pdf` to `~/Documents/outputs/` and commits/pushes **only** those explicit paths (`git commit --only -- outputs/KGrealis_cv.docx outputs/KGrealis_cv.pdf`).

## Rate Limiting

- Enforces a cross-process file-locked rate limit (`1.0 req/sec` via `fcntl.flock` and `time.monotonic()`) across OpenAlex, PubMed E-utilities, and R/Medicine HTTP requests, with exponential backoff on HTTP `5xx` and explicit `RateLimitError` on HTTP `429`.

## Common Mistakes

- **Skipping Confirmation on `Grealis K` Name Collisions**: Pre-2015 PubMed results include unrelated nursing articles by Katherine Grealis; always review `discover` output with Kyle before editing `cv_data.json`.
- **Browser Caching on `kylegrealis.com`**: Cloudflare Pages caches static PDFs for 4 hours (`max-age=14400`). Always run `sync` so the `?v=YYYY-MM-DD` query parameter in `index.qmd` and `docs/index.html` is updated.
- **Broad `git add -A` in `~/Documents`**: Never run `git add -A` in `~/Documents`; `sync` scopes git staging and commits strictly to `outputs/KGrealis_cv.docx` and `outputs/KGrealis_cv.pdf`.
