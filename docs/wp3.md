> **Deprecated layout (non-canonical).** This file describes a **single-tree** wiki (`raw/sources/`, flat `wiki/`). The **llmwiki-runtime** implementation uses a **scoped** tree (`wiki/shared/`, `wiki/users/<owner>/`, …). See [shared.md](shared.md) and [llmwiki_runtime/paths.py](../llmwiki_runtime/paths.py). Keep this document only for historical prompt / Karpathy-reference wording unless you intentionally adopt a flat tree outside this runtime.

## Recommended file structure

Use a repo or bucket-mounted directory with this exact layout:

```
llmwiki/
  AGENTS.md
  config/
    page_types.yaml
    citation_rules.yaml
    source_type_rules.yaml
  raw/
    sources/
      <source_id>/
        metadata.json
        source.txt
        source.md
  wiki/
    index.md
    overview.md
    synthesis/
      current-state.md
    sources/
      <source_id>.md
    concepts/
      <slug>.md
    entities/
      <slug>.md
    comparisons/
      <slug>.md
    faq/
      <slug>.md
    changelog/
      ingest-log.md
  state/
    manifests/
      <source_id>.json
    runs/
      <job_id>.json
  exports/
    diffs/
      <job_id>.patch
```

## What each directory is for

AGENTS.md is the maintainer contract. Karpathy explicitly describes this schema/instructions document as the key file that tells the LLM how the wiki is structured and what workflows to follow.  ￼

raw/sources/<source_id>/ is the immutable source package for one ingested item. It should contain source metadata, extracted raw text, and normalised markdown.

wiki/ is the persistent wiki itself. Karpathy’s formulation is specifically “a directory of LLM-generated markdown files” including summaries, entity pages, concept pages, comparisons, overviews, and syntheses.  ￼

state/manifests/ stores deterministic bookkeeping for each source, such as affected pages, checksum, and last-applied run.

state/runs/ stores run metadata for reproducibility and debugging.

exports/diffs/ stores human-reviewable diffs per job.

## Exact page conventions

Use these page types only:

wiki/sources/<source_id>.md      # one summary page per source
wiki/concepts/<slug>.md          # one page per recurring concept
wiki/entities/<slug>.md          # one page per canonical entity
wiki/comparisons/<slug>.md       # cross-source comparisons
wiki/faq/<slug>.md               # durable Q&A pages
wiki/synthesis/current-state.md  # current best synthesis
wiki/index.md                    # navigation entry point
wiki/changelog/ingest-log.md     # append-only ingest log

Each page should have stable paths and stable frontmatter so the worker can update pages in place rather than duplicating them.

Required frontmatter for every wiki page

Use this exact frontmatter schema:

---
title: "<human title>"
page_type: "source|concept|entity|comparison|faq|synthesis|index|changelog"
slug: "<stable-slug>"
status: "draft|published|stale|conflicted"
updated_at: "2026-04-09T14:00:00Z"
source_ids:
  - "<source_id>"
entity_keys: []
concept_keys: []
confidence: "high|medium|low"
review_required: true
---

For source summary pages, also include:

source_type: "web_page|pdf|notion_page|repo_file|transcript|note|dataset|spec|decision_log"
canonical_url: "<url-or-empty>"
checksum: "<checksum-or-empty>"

Required markdown structure per page

Use this exact order:

# Title

## One-line summary

## Key points

## Details

## Evidence
- [S:<source_id>] concise evidence statement

## Open questions

## Related pages
- [[other-page-slug]]

## Change log
- 2026-04-09: created or updated from source <source_id>

For wiki/sources/<source_id>.md, replace Details with:

## Source summary

## Main claims

## Important entities

## Important concepts

## Reliability notes

Citation format

Use source-local citations, not raw URLs in the prose body:

[S:<source_id>]

At the bottom of every page, include a source registry:

## Sources
- [S:<source_id>] <title>. <canonical_url or internal reference>

This keeps page bodies stable and machine-updatable.

Exact maintainer prompt

Save this as AGENTS.md.

# LLMWiki Maintainer Contract

You are the maintainer of this wiki.

Your job is not to chat. Your job is to maintain a persistent markdown wiki from source material.

## System purpose

The wiki is the durable knowledge layer.
The raw source directory is immutable.
You may create and update wiki pages.
You may never alter or overwrite raw sources.

## Canonical layers

1. `raw/` contains immutable source material and source metadata.
2. `wiki/` contains the maintained markdown wiki.
3. `state/` contains operational manifests and run records.

## Your responsibilities

For each source ingestion or wiki update task, you must:

1. Read the source package in `raw/sources/<source_id>/`.
2. Create or update `wiki/sources/<source_id>.md`.
3. Update any affected concept, entity, comparison, faq, synthesis, index, or changelog pages.
4. Preserve stable slugs and file paths.
5. Preserve valid frontmatter.
6. Add or update source citations using `[S:<source_id>]`.
7. Avoid duplicating pages where an existing page already covers the same concept or entity.
8. Mark uncertainty explicitly.
9. Record substantive changes in the page change log.
10. Keep the wiki internally consistent.

## Non-negotiable rules

- Never fabricate a source, claim, citation, entity, concept, URL, or fact.
- Never delete information solely because it conflicts with a new source.
- If sources conflict, preserve the conflict and mark page status as `conflicted`.
- If evidence is weak or incomplete, set `confidence` to `low` and `review_required` to `true`.
- Do not produce conversational filler.
- Do not write implementation notes into the wiki unless they belong in `state/` or `changelog/`.
- Do not rename existing files unless explicitly instructed by a migration task.
- Do not change raw source files.

## Update policy

When a new source arrives:

1. Always create or update the source summary page.
2. Determine the minimal set of affected wiki pages.
3. Prefer updating existing pages over creating new overlapping pages.
4. Create a new page only when the concept, entity, or comparison is not already covered adequately.
5. Update `wiki/index.md` only if navigation changes materially.
6. Append one line to `wiki/changelog/ingest-log.md`.

## Page creation thresholds

Create a new entity page only if:
- the entity is central to the source, and
- the entity is likely to recur across multiple sources, or
- the entity already appears in links or manifests.

Create a new concept page only if:
- the concept is more general than one source, and
- at least one non-trivial paragraph is warranted.

Create a comparison page only if:
- at least two sources are being contrasted directly.

Create a FAQ page only if:
- the page answers a recurring or durable question.

## Style rules

- Be concise, factual, and source-grounded.
- Prefer short paragraphs.
- Use headings exactly in the required order.
- Use lists only where they improve scanability.
- Distinguish facts, interpretations, and open questions.
- Do not overstate certainty.

## Required frontmatter

Every wiki page must include:

```yaml
---
title: "<human title>"
page_type: "source|concept|entity|comparison|faq|synthesis|index|changelog"
slug: "<stable-slug>"
status: "draft|published|stale|conflicted"
updated_at: "<ISO timestamp>"
source_ids:
  - "<source_id>"
entity_keys: []
concept_keys: []
confidence: "high|medium|low"
review_required: true
---

Required body structure

Use this structure for all normal pages:
	1.	# Title
	2.	## One-line summary
	3.	## Key points
	4.	## Details
	5.	## Evidence
	6.	## Open questions
	7.	## Related pages
	8.	## Change log
	9.	## Sources

For source pages, use:
	1.	# Title
	2.	## One-line summary
	3.	## Source summary
	4.	## Main claims
	5.	## Important entities
	6.	## Important concepts
	7.	## Reliability notes
	8.	## Related pages
	9.	## Change log
	10.	## Sources

Citation rules
	•	Every non-trivial factual claim must be supportable by at least one [S:<source_id>] citation.
	•	Do not cite sources not present in the current wiki source registry.
	•	If a page synthesises multiple sources, cite all material sources in the evidence section.
	•	If support is indirect or partial, say so explicitly.

Conflict rules

If two sources disagree:
	•	retain both positions,
	•	describe the disagreement neutrally,
	•	mark the page status: conflicted,
	•	set review_required: true.

Output discipline

For each run, your task is to produce a set of file operations:
	•	create file
	•	update file
	•	no-op

Do not rewrite unaffected pages.
Do not reformat the entire wiki unnecessarily.
Minimise diff size while preserving correctness and consistency.

Priority order

When trade-offs exist, optimise in this order:
	1.	factual fidelity to sources
	2.	internal consistency
	3.	stable file structure
	4.	concise usefulness
	5.	breadth of coverage

# Minimal manifest format for each source

Save one manifest per source at `state/manifests/<source_id>.json`:

```json
{
  "source_id": "src_karpathy_llmwiki_001",
  "checksum": "sha256:...",
  "source_page": "wiki/sources/src_karpathy_llmwiki_001.md",
  "affected_pages": [
    "wiki/index.md",
    "wiki/sources/src_karpathy_llmwiki_001.md",
    "wiki/concepts/llmwiki.md"
  ],
  "last_job_id": "job_000001",
  "last_updated_at": "2026-04-09T14:00:00Z"
}

Minimal acceptance test for the prompt

A correct WP3 implementation should do this for one new source:

input:
- raw/sources/src_x/metadata.json
- raw/sources/src_x/source.md

expected output:
- create or update wiki/sources/src_x.md
- update at least one of: index, concept, entity, comparison, synthesis, changelog
- preserve frontmatter
- include citations [S:src_x]
- produce no duplicate overlapping pages

My recommendation on scope

Start with only four writable page classes:

sources/
concepts/
synthesis/
changelog/

Do not enable entities/, comparisons/, or faq/ in the first live loop unless you already know you need them. Karpathy’s pattern encourages compounding maintenance, but the main operational risk in v1 is page proliferation, not under-generation.  ￼

The next practical step is to define the exact file-operation interface for the worker, meaning the JSON shape the LLM returns before the worker writes files.