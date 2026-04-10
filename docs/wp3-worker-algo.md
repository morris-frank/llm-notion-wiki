Below is a concrete worker algorithm for WP3: validate the LLM plan, apply file operations safely, compute diffs, and sync state back into Notion.

Two design constraints drive it:
	•	Karpathy’s pattern treats the wiki as a maintained directory of markdown files, not an ephemeral chat answer.  ￼
	•	In Notion, the control plane should be read and updated through data sources and page property updates, with schemas matching the parent data source.  ￼

### Paths (scoped layout in llmwiki-runtime)

Artifacts are **per scope** (shared vs `wiki/users/<owner>/`), not a single global `exports/` or `state/manifests/` root:

- **Manifests:** `state/manifests/shared/<source_id>.json` or `state/manifests/users/<owner>/<source_id>.json`
- **Run records:** `state/runs/shared/<job_id>.json` or `state/runs/users/<owner>/<job_id>.json`
- **Diffs:** `exports/diffs/shared/<job_id>.patch` or `exports/diffs/users/<owner>/<job_id>.patch`
- **Raw sources:** `raw/shared/canonical/<source_id>/` or `raw/users/<owner>/canonical/<source_id>/`

See [shared.md](shared.md).

1. Worker responsibilities

The worker should execute one job at a time and do exactly six phases:
	1.	load job + source context
	2.	assemble LLM input bundle
	3.	obtain JSON operation plan
	4.	validate the plan
	5.	apply the plan to disk
	6.	sync outcomes back to Notion

No phase should directly bypass the previous one.

2. Exact runtime inputs

Per job, the worker needs:

{
  "job_id": "job_000001",
  "job_type": "update_wiki",
  "source_id": "src_karpathy_llmwiki_001",
  "source_page_id": "<notion-page-id>",
  "policy_page_id": "<notion-page-id-or-null>",
  "wiki_root": "/path/to/llmwiki",
  "raw_source_dir": "/path/to/llmwiki/raw/sources/src_karpathy_llmwiki_001",
  "manifest_path": "/path/to/llmwiki/state/manifests/src_karpathy_llmwiki_001.json"
}

3. Worker state machine

Use this exact state machine:

queued
→ running
→ validating_plan
→ applying_changes
→ syncing_state
→ succeeded

or

queued
→ running
→ failed

If you keep FEATURE_JOB_CONTROL=1, write these states back through page property updates in Notion. Notion’s page update API supports updating page properties for rows that belong to data sources.  ￼

4. End-to-end algorithm

Phase A. Claim and prepare the job
	1.	Query Jobs for one queued job.
	2.	Set:
	•	Job Status=running
	•	Started At=<now>
	•	Locked=true
	•	optionally Worker Name=<worker-id>
	3.	Read:
	•	the job row
	•	the target source row
	•	the current manifest, if it exists
	4.	Load source artefacts from:
	•	`raw/shared/canonical/<source_id>/` or `raw/users/<owner>/canonical/<source_id>/` (metadata.json, source.md)

Use data-source queries for queue retrieval rather than search. Notion’s current API is explicitly data-source centric.  ￼

Phase B. Build the LLM input bundle

Construct one JSON bundle for the model:

{
  "job": {...},
  "source": {
    "source_id": "src_karpathy_llmwiki_001",
    "metadata": {...},
    "content_markdown": "..."
  },
  "current_manifest": {...},
  "existing_pages": {
    "source_page": "...existing content or null...",
    "candidate_pages": [
      {
        "path": "wiki/concepts/llmwiki.md",
        "content": "..."
      },
      {
        "path": "wiki/index.md",
        "content": "..."
      },
      {
        "path": "wiki/changelog/ingest-log.md",
        "content": "..."
      }
    ]
  },
  "maintainer_contract": "...AGENTS.md text...",
  "operation_schema": "...JSON schema text..."
}

Candidate pages should be the minimal plausible set, not the whole wiki. That is consistent with Karpathy’s model of targeted maintenance of multiple pages per source, not global rewrites.  ￼

Phase C. Call the LLM and request only JSON

Prompt the model with:
	•	AGENTS.md
	•	the file-operation contract
	•	the source bundle
	•	existing candidate pages
	•	an instruction to return only valid JSON

Store raw model output in state/runs/<job_id>.json.

Phase D. Validate the returned plan

Validation is two-stage:

D1. Schema validation

Validate against the JSON Schema you defined earlier.

Reject immediately if:
	•	invalid JSON
	•	unknown operation type
	•	missing required fields
	•	extra top-level fields

D2. Semantic validation

Reject immediately if any of these fail:
	•	any path is outside wiki/
	•	any path contains .. or is absolute
	•	page_type and path disagree
	•	touched_paths does not match the actual touched set
	•	manifest_update.affected_pages omits a touched path
	•	create_file targets an existing file
	•	replace_file targets a missing file
	•	append_block is used outside wiki/changelog/
	•	required frontmatter is missing for create_file/replace_file
	•	frontmatter page_type disagrees with operation page_type
	•	frontmatter source_ids does not include current source_id

For patch_sections:
	•	the target file must already exist
	•	the referenced section headings must exist
	•	upsert_bullet may create or update only one list item under the named section

If validation fails:
	•	write Job Status=failed
	•	populate Error Class=validation
	•	populate Error Message
	•	set Locked=false

5. Apply algorithm by operation type

Maintain an in-memory map of file contents first. Write to disk only after all operations validate.

create_file

Algorithm:
	1.	ensure file absent
	2.	ensure parent directory exists
	3.	parse frontmatter
	4.	ensure required sections exist
	5.	insert full content into in-memory file map

patch_sections

Algorithm:
	1.	load target file
	2.	split into frontmatter + body
	3.	parse markdown into section blocks by heading
	4.	for each section_patch:
	•	locate exact heading
	•	apply action:
	•	replace: replace section body only
	•	append: append block to section body
	•	prepend: prepend block to section body
	•	upsert_bullet: update matching bullet by match_key, else append
	5.	reassemble file
	6.	preserve heading order outside touched sections

This is the preferred default because it minimises drift and diff size.

append_block

Algorithm:
	1.	ensure target exists
	2.	append one newline plus content
	3.	do not otherwise reformat file

no_op

Do nothing.

replace_file

For v1, I recommend rejecting it entirely or feature-gating it behind an explicit unsafe mode. It produces unnecessarily large diffs too easily.

6. Two-phase commit for filesystem writes

Use this exact write pattern:
	1.	compute all new file contents in memory
	2.	validate all resulting files again
	3.	write each changed file to a temp file
	4.	fsync temp file
	5.	atomically rename temp file into place
	6.	only after all writes succeed, update manifest and Notion

If any write fails:
	•	do not partially update Notion
	•	leave job failed
	•	preserve temp artefacts for debugging

7. Diff generation

For each changed file:
	1.	load old content
	2.	compare with new content
	3.	if unchanged, drop the operation from the applied set
	4.	if changed, compute unified diff and save to:
	•	exports/diffs/<job_id>.patch

Also store a machine-readable run record:

{
  "job_id": "job_000001",
  "source_id": "src_karpathy_llmwiki_001",
  "applied_operations": [...],
  "changed_files": [
    {
      "path": "wiki/concepts/llmwiki.md",
      "old_sha256": "...",
      "new_sha256": "..."
    }
  ],
  "warnings": []
}

8. Manifest update algorithm

After successful write:
	1.	create or update state/manifests/<source_id>.json
	2.	set:
	•	source_id
	•	checksum
	•	source_page
	•	affected_pages
	•	last_job_id
	•	last_updated_at

Example:

{
  "source_id": "src_karpathy_llmwiki_001",
  "checksum": "sha256:...",
  "source_page": "wiki/sources/src_karpathy_llmwiki_001.md",
  "affected_pages": [
    "wiki/sources/src_karpathy_llmwiki_001.md",
    "wiki/concepts/llmwiki.md",
    "wiki/changelog/ingest-log.md"
  ],
  "last_job_id": "job_000001",
  "last_updated_at": "2026-04-09T14:00:00Z"
}

9. Notion sync algorithm

Once filesystem state is committed, sync the control plane.

Update the source row

Set relevant properties such as:
	•	Source Status=processed
	•	Last Processed At=<now>
	•	Source Summary Pointer=<path or URL>
	•	optionally Trigger Regeneration=false

Upsert wiki rows

For each changed wiki page:
	1.	derive:
	•	title
	•	slug
	•	page type
	•	status
	•	confidence
	•	review flag
	2.	query Wiki Pages by slug
	3.	if missing, create a row
	4.	if present, update it
	5.	if link-graph features are on, update relations:
	•	Backing Sources
	•	Latest Job

Pages in Notion data sources must conform to the parent property schema, and updating page properties is done through the page update endpoint.  ￼

Update the job row

Set:
	•	Job Status=succeeded
	•	Finished At=<now>
	•	Duration Ms=<computed>
	•	Output Pointer=<run record path>
	•	Diff Pointer=<diff path>
	•	Locked=false

If the run produced warnings or low-confidence pages:
	•	set Error Class only for actual failures, not warnings
	•	use wiki-page review flags for content uncertainty instead

10. Exact worker pseudocode

function run_job(job_id):
    claim_job(job_id)

    ctx = load_context(job_id)
    llm_input = build_llm_input(ctx)

    llm_raw = call_model(llm_input)
    save_run_artifact(job_id, "llm_raw.json", llm_raw)

    plan = parse_json(llm_raw)
    validate_schema(plan)
    validate_semantics(plan, ctx)

    set_job_status(job_id, "validating_plan")

    file_state = load_current_files(plan, ctx)
    new_state = apply_operations(plan, file_state, ctx)
    validate_resulting_files(new_state, ctx)

    set_job_status(job_id, "applying_changes")

    changed_files = atomic_write_all(new_state)
    diff_path = write_diff(job_id, changed_files)
    manifest = update_manifest(ctx, plan, changed_files)
    run_record = write_run_record(job_id, plan, changed_files, manifest)

    set_job_status(job_id, "syncing_state")

    sync_source_row(ctx, manifest)
    sync_wiki_rows(ctx, changed_files, plan)
    sync_job_success(job_id, diff_path, run_record)

    return success

Error path:

except ValidationError as e:
    sync_job_failure(job_id, class="validation", message=e.message)

except LLMError as e:
    sync_job_failure(job_id, class="model_failure", message=e.message)

except IOError as e:
    sync_job_failure(job_id, class="external_io", message=e.message)

except Exception as e:
    sync_job_failure(job_id, class="unknown", message=str(e))

11. Recommended v1 restrictions

For the first live version, enforce these hard limits:
	•	only process one job at a time
	•	only allow page types:
	•	source
	•	concept
	•	synthesis
	•	changelog
	•	reject replace_file
	•	reject edits to more than 5 files per run
	•	reject any single file over a size threshold, e.g. 200 KB
	•	reject plans with more than 20 section patches

This is the conservative choice. The unreasonable assumption would be that the model will behave well under an unconstrained write surface.

12. Acceptance criteria for the worker

A worker implementation is good enough for WP3 if all of these pass:
	1.	Given one processed source, it can create one source page and update one existing concept page.
	2.	It refuses invalid JSON or unsafe paths.
	3.	It never writes outside wiki/.
	4.	It produces a diff artifact for every successful run.
	5.	It updates Jobs, Sources, and Wiki Pages rows consistently in Notion.
	6.	Re-running the same job is idempotent, yielding either no_op or zero-content-change writes.

13. Small but important implementation choice

Have the LLM propose candidate operations, but let the worker compute:
	•	content hashes
	•	timestamps
	•	final diff artifacts
	•	job duration
	•	some derived Notion fields

That separation reduces gratuitous churn and makes reruns more stable.