#!/usr/bin/env bash
set -euo pipefail

# bootstrap_llmwiki_notion_dynamic.sh
#
# Dynamically bootstraps a Notion-based LLMWiki control plane.
# The schema is assembled from feature flags rather than hardcoded
# as one static payload.
#
# Required env:
#   NOTION_TOKEN
#   PARENT_PAGE_ID
#
# Optional env:
#   NOTION_VERSION=2026-03-11
#   API_BASE=https://api.notion.com/v1
#
# Feature flags:
#   FEATURE_SOURCE_ENRICHMENT=1
#   FEATURE_SOURCE_DIAGNOSTICS=1
#   FEATURE_EDITORIAL_WORKFLOW=1
#   FEATURE_FRESHNESS=1
#   FEATURE_CONFIDENCE=0
#   FEATURE_LINK_GRAPH=1
#   FEATURE_ENTITIES=0
#   FEATURE_QUESTIONS=0
#   FEATURE_JOB_CONTROL=1
#   FEATURE_POLICY_ENGINE=1
#
# Output:
#   Prints export lines for created IDs.

: "${NOTION_TOKEN:?Missing NOTION_TOKEN}"
: "${PARENT_PAGE_ID:?Missing PARENT_PAGE_ID}"

NOTION_VERSION="${NOTION_VERSION:-2026-03-11}"
API_BASE="${API_BASE:-https://api.notion.com/v1}"

FEATURE_SOURCE_ENRICHMENT="${FEATURE_SOURCE_ENRICHMENT:-1}"
FEATURE_SOURCE_DIAGNOSTICS="${FEATURE_SOURCE_DIAGNOSTICS:-1}"
FEATURE_EDITORIAL_WORKFLOW="${FEATURE_EDITORIAL_WORKFLOW:-1}"
FEATURE_FRESHNESS="${FEATURE_FRESHNESS:-1}"
FEATURE_CONFIDENCE="${FEATURE_CONFIDENCE:-0}"
FEATURE_LINK_GRAPH="${FEATURE_LINK_GRAPH:-1}"
FEATURE_ENTITIES="${FEATURE_ENTITIES:-0}"
FEATURE_QUESTIONS="${FEATURE_QUESTIONS:-0}"
FEATURE_JOB_CONTROL="${FEATURE_JOB_CONTROL:-1}"
FEATURE_POLICY_ENGINE="${FEATURE_POLICY_ENGINE:-1}"

POLICY_PROMPT_BUNDLE_URL="${POLICY_PROMPT_BUNDLE_URL:-https://example.com/policies/prompt-bundle-v1.md}"
POLICY_CITATION_URL="${POLICY_CITATION_URL:-https://example.com/policies/citation-policy-v1.md}"
POLICY_TEMPLATE_URL="${POLICY_TEMPLATE_URL:-https://example.com/policies/page-template-v1.md}"
POLICY_CONFLICT_URL="${POLICY_CONFLICT_URL:-https://example.com/policies/conflict-resolution-v1.md}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

require_cmd curl
require_cmd jq

api() {
  local method="$1"
  local path="$2"
  local data_file="${3:-}"

  if [[ -n "$data_file" ]]; then
    curl -fsS "${API_BASE}${path}" \
      -X "$method" \
      -H "Authorization: Bearer ${NOTION_TOKEN}" \
      -H "Notion-Version: ${NOTION_VERSION}" \
      -H "Content-Type: application/json" \
      --data @"$data_file"
  else
    curl -fsS "${API_BASE}${path}" \
      -X "$method" \
      -H "Authorization: Bearer ${NOTION_TOKEN}" \
      -H "Notion-Version: ${NOTION_VERSION}" \
      -H "Content-Type: application/json"
  fi
}

json_write() {
  local file="$1"
  cat > "$file"
}

json_init_object() {
  echo '{}' > "$1"
}

add_prop() {
  local file="$1"
  local key="$2"
  local value_json="$3"

  jq --arg k "$key" --argjson v "$value_json" \
    '. + {($k): $v}' "$file" > "${file}.tmp"
  mv "${file}.tmp" "$file"
}

append_select_option() {
  local file="$1"
  local prop="$2"
  local option_json="$3"

  jq --arg p "$prop" --argjson opt "$option_json" \
    '.[$p].select.options += [$opt]' "$file" > "${file}.tmp"
  mv "${file}.tmp" "$file"
}

mk_payload() {
  local out="$1"
  local title="$2"
  local props_file="$3"

  jq -n \
    --arg title "$title" \
    --slurpfile props "$props_file" \
    '{
      title: [
        {
          type: "text",
          text: { content: $title }
        }
      ],
      properties: $props[0]
    }' > "$out"
}

patch_props() {
  local out="$1"
  local props_file="$2"

  jq -n --slurpfile props "$props_file" '{properties: $props[0]}' > "$out"
}

create_page_payload() {
  local out="$1"
  local ds_id="$2"
  local props_file="$3"

  jq -n \
    --arg ds "$ds_id" \
    --slurpfile props "$props_file" \
    '{
      parent: { data_source_id: $ds },
      properties: $props[0]
    }' > "$out"
}

# ---------------------------
# Build property JSON objects
# ---------------------------

SOURCES_PROPS="$TMP_DIR/sources_props.json"
WIKI_PROPS="$TMP_DIR/wiki_props.json"
JOBS_PROPS="$TMP_DIR/jobs_props.json"
POLICIES_PROPS="$TMP_DIR/policies_props.json"

json_init_object "$SOURCES_PROPS"
json_init_object "$WIKI_PROPS"
json_init_object "$JOBS_PROPS"
json_init_object "$POLICIES_PROPS"

# Sources: always-on
add_prop "$SOURCES_PROPS" "Source Title" '{ "title": {} }'
add_prop "$SOURCES_PROPS" "Source ID" '{ "rich_text": {} }'
add_prop "$SOURCES_PROPS" "Source Type" '{
  "select": { "options": [
    { "name": "notion_page", "color": "blue" },
    { "name": "pdf", "color": "red" },
    { "name": "web_page", "color": "green" },
    { "name": "repo_file", "color": "purple" },
    { "name": "transcript", "color": "orange" },
    { "name": "note", "color": "yellow" },
    { "name": "dataset", "color": "pink" },
    { "name": "spec", "color": "gray" },
    { "name": "decision_log", "color": "brown" }
  ] }
}'
add_prop "$SOURCES_PROPS" "Canonical URL" '{ "url": {} }'
add_prop "$SOURCES_PROPS" "Trust Level" '{
  "select": { "options": [
    { "name": "primary", "color": "green" },
    { "name": "internal", "color": "blue" },
    { "name": "secondary", "color": "yellow" },
    { "name": "low_confidence", "color": "red" }
  ] }
}'
add_prop "$SOURCES_PROPS" "Source Status" '{
  "select": { "options": [
    { "name": "queued", "color": "gray" },
    { "name": "fetching", "color": "blue" },
    { "name": "parsed", "color": "purple" },
    { "name": "processed", "color": "green" },
    { "name": "failed", "color": "red" }
  ] }
}'
add_prop "$SOURCES_PROPS" "Imported At" '{ "date": {} }'

# Sources: optional
if [[ "$FEATURE_SOURCE_ENRICHMENT" == "1" ]]; then
  add_prop "$SOURCES_PROPS" "External File Key" '{ "rich_text": {} }'
  add_prop "$SOURCES_PROPS" "Source Checksum" '{ "rich_text": {} }'
  add_prop "$SOURCES_PROPS" "Project" '{ "select": { "options": [] } }'
  add_prop "$SOURCES_PROPS" "Topic Tags" '{ "multi_select": { "options": [] } }'
  add_prop "$SOURCES_PROPS" "Language" '{ "select": { "options": [] } }'
  add_prop "$SOURCES_PROPS" "Last Seen At" '{ "date": {} }'
  add_prop "$SOURCES_PROPS" "Content Version" '{ "number": { "format": "number" } }'
  add_prop "$SOURCES_PROPS" "Freshness SLA Days" '{ "number": { "format": "number" } }'
fi

if [[ "$FEATURE_SOURCE_DIAGNOSTICS" == "1" ]]; then
  add_prop "$SOURCES_PROPS" "Parse Error" '{ "rich_text": {} }'
  add_prop "$SOURCES_PROPS" "Last Error At" '{ "date": {} }'
  add_prop "$SOURCES_PROPS" "Raw Text Pointer" '{ "url": {} }'
  add_prop "$SOURCES_PROPS" "Normalised Markdown Pointer" '{ "url": {} }'
  add_prop "$SOURCES_PROPS" "Source Summary Pointer" '{ "url": {} }'
fi

if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
  add_prop "$SOURCES_PROPS" "Review Required" '{ "checkbox": {} }'
fi

if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
  add_prop "$SOURCES_PROPS" "Last Parsed At" '{ "date": {} }'
  add_prop "$SOURCES_PROPS" "Last Processed At" '{ "date": {} }'
  add_prop "$SOURCES_PROPS" "Trigger Regeneration" '{ "checkbox": {} }'
fi

# Wiki Pages: always-on
add_prop "$WIKI_PROPS" "Wiki Title" '{ "title": {} }'
add_prop "$WIKI_PROPS" "Wiki Slug" '{ "rich_text": {} }'
add_prop "$WIKI_PROPS" "Wiki Type" '{
  "select": { "options": [
    { "name": "entity", "color": "blue" },
    { "name": "concept", "color": "purple" },
    { "name": "source_summary", "color": "green" },
    { "name": "comparison", "color": "orange" },
    { "name": "synthesis", "color": "pink" },
    { "name": "faq", "color": "yellow" }
  ] }
}'
add_prop "$WIKI_PROPS" "Wiki Status" '{
  "select": { "options": [
    { "name": "draft", "color": "gray" },
    { "name": "published", "color": "green" },
    { "name": "archived", "color": "brown" }
  ] }
}'
add_prop "$WIKI_PROPS" "Canonical Markdown Path" '{ "rich_text": {} }'
add_prop "$WIKI_PROPS" "Summary" '{ "rich_text": {} }'

if [[ "$FEATURE_SOURCE_ENRICHMENT" == "1" ]]; then
  add_prop "$WIKI_PROPS" "Published URL" '{ "url": {} }'
fi

if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
  add_prop "$WIKI_PROPS" "Needs Human Review" '{ "checkbox": {} }'
  add_prop "$WIKI_PROPS" "Last Reviewed At" '{ "date": {} }'
  add_prop "$WIKI_PROPS" "Last Published At" '{ "date": {} }'
  add_prop "$WIKI_PROPS" "Editorial State" '{
    "select": { "options": [
      { "name": "untouched", "color": "gray" },
      { "name": "ai_draft", "color": "blue" },
      { "name": "in_review", "color": "yellow" },
      { "name": "approved", "color": "green" },
      { "name": "rejected", "color": "red" }
    ] }
  }'
fi

if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
  add_prop "$WIKI_PROPS" "Last Generated At" '{ "date": {} }'
  add_prop "$WIKI_PROPS" "Regeneration Reason" '{
    "select": { "options": [
      { "name": "source_changed", "color": "blue" },
      { "name": "schedule", "color": "gray" },
      { "name": "manual", "color": "orange" },
      { "name": "schema_changed", "color": "purple" }
    ] }
  }'
  add_prop "$WIKI_PROPS" "Freshness Target Days" '{ "number": { "format": "number" } }'
  append_select_option "$WIKI_PROPS" "Wiki Status" '{ "name": "stale", "color": "yellow" }'
fi

if [[ "$FEATURE_CONFIDENCE" == "1" ]]; then
  add_prop "$WIKI_PROPS" "Confidence Score" '{ "number": { "format": "number" } }'
  add_prop "$WIKI_PROPS" "Conflict Flag" '{ "checkbox": {} }'
fi

if [[ "$FEATURE_LINK_GRAPH" == "1" ]]; then
  add_prop "$WIKI_PROPS" "Source Count" '{ "number": { "format": "number" } }'
  add_prop "$WIKI_PROPS" "Link Count" '{ "number": { "format": "number" } }'
fi

# Jobs: always-on
add_prop "$JOBS_PROPS" "Job Title" '{ "title": {} }'
add_prop "$JOBS_PROPS" "Job ID" '{ "rich_text": {} }'
add_prop "$JOBS_PROPS" "Job Type" '{
  "select": { "options": [
    { "name": "ingest_source", "color": "blue" },
    { "name": "parse_source", "color": "purple" },
    { "name": "summarise_source", "color": "green" },
    { "name": "update_wiki", "color": "orange" }
  ] }
}'
add_prop "$JOBS_PROPS" "Job Status" '{
  "select": { "options": [
    { "name": "queued", "color": "gray" },
    { "name": "running", "color": "blue" },
    { "name": "succeeded", "color": "green" },
    { "name": "failed", "color": "red" }
  ] }
}'
add_prop "$JOBS_PROPS" "Queue Timestamp" '{ "date": {} }'

if [[ "$FEATURE_QUESTIONS" == "1" ]]; then
  append_select_option "$JOBS_PROPS" "Job Type" '{ "name": "answer_question", "color": "brown" }'
fi

if [[ "$FEATURE_JOB_CONTROL" == "1" ]]; then
  add_prop "$JOBS_PROPS" "Trigger Type" '{
    "select": { "options": [
      { "name": "webhook", "color": "blue" },
      { "name": "schedule", "color": "gray" },
      { "name": "manual", "color": "orange" },
      { "name": "dependency", "color": "purple" },
      { "name": "repair", "color": "red" }
    ] }
  }'
  add_prop "$JOBS_PROPS" "Trigger Event ID" '{ "rich_text": {} }'
  add_prop "$JOBS_PROPS" "Priority" '{
    "select": { "options": [
      { "name": "urgent", "color": "red" },
      { "name": "high", "color": "orange" },
      { "name": "normal", "color": "blue" },
      { "name": "low", "color": "gray" }
    ] }
  }'
  add_prop "$JOBS_PROPS" "Attempt Count" '{ "number": { "format": "number" } }'
  add_prop "$JOBS_PROPS" "Max Attempts" '{ "number": { "format": "number" } }'
  add_prop "$JOBS_PROPS" "Started At" '{ "date": {} }'
  add_prop "$JOBS_PROPS" "Finished At" '{ "date": {} }'
  add_prop "$JOBS_PROPS" "Duration Ms" '{ "number": { "format": "number" } }'
  add_prop "$JOBS_PROPS" "Worker Name" '{ "rich_text": {} }'
  add_prop "$JOBS_PROPS" "Idempotency Key" '{ "rich_text": {} }'
  add_prop "$JOBS_PROPS" "Error Class" '{
    "select": { "options": [
      { "name": "rate_limit", "color": "yellow" },
      { "name": "validation", "color": "orange" },
      { "name": "permissions", "color": "red" },
      { "name": "parsing", "color": "purple" },
      { "name": "model_failure", "color": "pink" },
      { "name": "external_io", "color": "blue" },
      { "name": "unknown", "color": "gray" }
    ] }
  }'
  add_prop "$JOBS_PROPS" "Error Message" '{ "rich_text": {} }'
  add_prop "$JOBS_PROPS" "Retry After Seconds" '{ "number": { "format": "number" } }'
  add_prop "$JOBS_PROPS" "Output Pointer" '{ "url": {} }'
  add_prop "$JOBS_PROPS" "Diff Pointer" '{ "url": {} }'
  add_prop "$JOBS_PROPS" "Locked" '{ "checkbox": {} }'
fi

# Policies: always-on
add_prop "$POLICIES_PROPS" "Policy Name" '{ "title": {} }'
add_prop "$POLICIES_PROPS" "Policy Version" '{ "rich_text": {} }'
add_prop "$POLICIES_PROPS" "Policy Scope" '{
  "select": { "options": [
    { "name": "global", "color": "blue" },
    { "name": "source_type", "color": "purple" },
    { "name": "wiki_type", "color": "green" },
    { "name": "project", "color": "orange" }
  ] }
}'
add_prop "$POLICIES_PROPS" "Active" '{ "checkbox": {} }'

if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
  add_prop "$POLICIES_PROPS" "Prompt Bundle Pointer" '{ "url": {} }'
  add_prop "$POLICIES_PROPS" "Citation Policy Pointer" '{ "url": {} }'
  add_prop "$POLICIES_PROPS" "Page Template Pointer" '{ "url": {} }'
  add_prop "$POLICIES_PROPS" "Max Source Count" '{ "number": { "format": "number" } }'
fi

if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
  add_prop "$POLICIES_PROPS" "Auto Publish Allowed" '{ "checkbox": {} }'
  add_prop "$POLICIES_PROPS" "Requires Human Review" '{ "checkbox": {} }'
fi

if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
  add_prop "$POLICIES_PROPS" "Regeneration Threshold Days" '{ "number": { "format": "number" } }'
fi

if [[ "$FEATURE_CONFIDENCE" == "1" ]]; then
  add_prop "$POLICIES_PROPS" "Conflict Resolution Pointer" '{ "url": {} }'
fi

if [[ "$FEATURE_SOURCE_ENRICHMENT" == "1" ]]; then
  add_prop "$POLICIES_PROPS" "Updated At" '{ "date": {} }'
  add_prop "$POLICIES_PROPS" "Notes" '{ "rich_text": {} }'
fi

# ---------------------------
# Create database + data sources
# ---------------------------

CREATE_DB_PAYLOAD="$TMP_DIR/create_db.json"
jq -n \
  --arg page_id "$PARENT_PAGE_ID" \
  --slurpfile props "$SOURCES_PROPS" \
  '{
    parent: { type: "page_id", page_id: $page_id },
    title: [{ type: "text", text: { content: "LLMWiki Control Plane" } }],
    description: [{ type: "text", text: { content: "Operational control plane for a Notion-driven LLMWiki system." } }],
    is_inline: false,
    initial_data_source: {
      title: [{ type: "text", text: { content: "Sources" } }],
      properties: $props[0]
    }
  }' > "$CREATE_DB_PAYLOAD"

DB_RESP="$(api POST /databases "$CREATE_DB_PAYLOAD")"
CONTROL_DB_ID="$(jq -r '.id' <<<"$DB_RESP")"
SOURCES_DS_ID="$(
  jq -r '
    if .initial_data_source.id then .initial_data_source.id
    elif (.data_sources | type) == "array" and (.data_sources | length) > 0 then .data_sources[0].id
    else empty end
  ' <<<"$DB_RESP"
)"
[[ -n "$SOURCES_DS_ID" ]] || { echo "Could not determine SOURCES_DS_ID" >&2; exit 1; }

create_ds() {
  local title="$1"
  local props_file="$2"
  local out="$TMP_DIR/${title// /_}.json"

  mk_payload "$out" "$title" "$props_file"
  jq --arg db "$CONTROL_DB_ID" '. + {parent: {database_id: $db}}' "$out" > "${out}.tmp"
  mv "${out}.tmp" "$out"
  api POST /data_sources "$out"
}

WIKI_RESP="$(create_ds "Wiki Pages" "$WIKI_PROPS")"
WIKI_DS_ID="$(jq -r '.id' <<<"$WIKI_RESP")"

JOBS_RESP="$(create_ds "Jobs" "$JOBS_PROPS")"
JOBS_DS_ID="$(jq -r '.id' <<<"$JOBS_RESP")"

POLICIES_RESP="$(create_ds "Policies" "$POLICIES_PROPS")"
POLICIES_DS_ID="$(jq -r '.id' <<<"$POLICIES_RESP")"

ENTITIES_DS_ID=""
QUESTIONS_DS_ID=""

if [[ "$FEATURE_ENTITIES" == "1" ]]; then
  ENTITIES_PROPS="$TMP_DIR/entities_props.json"
  json_init_object "$ENTITIES_PROPS"
  add_prop "$ENTITIES_PROPS" "Entity Name" '{ "title": {} }'
  add_prop "$ENTITIES_PROPS" "Entity Type" '{
    "select": { "options": [
      { "name": "person", "color": "blue" },
      { "name": "company", "color": "green" },
      { "name": "project", "color": "orange" },
      { "name": "concept", "color": "purple" },
      { "name": "technology", "color": "pink" },
      { "name": "location", "color": "yellow" },
      { "name": "document", "color": "gray" }
    ] }
  }'
  add_prop "$ENTITIES_PROPS" "Canonical Entity ID" '{ "rich_text": {} }'
  ENTITIES_RESP="$(create_ds "Entities" "$ENTITIES_PROPS")"
  ENTITIES_DS_ID="$(jq -r '.id' <<<"$ENTITIES_RESP")"
fi

if [[ "$FEATURE_QUESTIONS" == "1" ]]; then
  QUESTIONS_PROPS="$TMP_DIR/questions_props.json"
  json_init_object "$QUESTIONS_PROPS"
  add_prop "$QUESTIONS_PROPS" "Question" '{ "title": {} }'
  add_prop "$QUESTIONS_PROPS" "Question ID" '{ "rich_text": {} }'
  add_prop "$QUESTIONS_PROPS" "Question Status" '{
    "select": { "options": [
      { "name": "queued", "color": "gray" },
      { "name": "answered", "color": "green" },
      { "name": "archived", "color": "brown" }
    ] }
  }'
  QUESTIONS_RESP="$(create_ds "Questions" "$QUESTIONS_PROPS")"
  QUESTIONS_DS_ID="$(jq -r '.id' <<<"$QUESTIONS_RESP")"
fi

# ---------------------------
# Patch relations conditionally
# ---------------------------

patch_relation_prop() {
  local source_ds_id="$1"
  local prop_name="$2"
  local target_ds_id="$3"
  local mode="$4"
  local patch_file="$TMP_DIR/patch_${source_ds_id}_${prop_name// /_}.json"

  if [[ "$mode" == "dual" ]]; then
    jq -n --arg p "$prop_name" --arg target "$target_ds_id" \
      '{properties: {($p): {relation: {data_source_id: $target, dual_property: {}}}}}' > "$patch_file"
  else
    jq -n --arg p "$prop_name" --arg target "$target_ds_id" \
      '{properties: {($p): {relation: {data_source_id: $target, single_property: {}}}}}' > "$patch_file"
  fi

  api PATCH "/data_sources/${source_ds_id}" "$patch_file" >/dev/null
}

if [[ "$FEATURE_LINK_GRAPH" == "1" ]]; then
  patch_relation_prop "$SOURCES_DS_ID" "Related Wiki Pages" "$WIKI_DS_ID" "single"
  patch_relation_prop "$SOURCES_DS_ID" "Latest Job" "$JOBS_DS_ID" "single"

  patch_relation_prop "$WIKI_DS_ID" "Backing Sources" "$SOURCES_DS_ID" "dual"
  patch_relation_prop "$WIKI_DS_ID" "Latest Job" "$JOBS_DS_ID" "single"

  patch_relation_prop "$JOBS_DS_ID" "Target Source" "$SOURCES_DS_ID" "single"
  patch_relation_prop "$JOBS_DS_ID" "Target Wiki Page" "$WIKI_DS_ID" "single"
fi

if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
  patch_relation_prop "$JOBS_DS_ID" "Policy Version Ref" "$POLICIES_DS_ID" "single"
fi

if [[ "$FEATURE_ENTITIES" == "1" && -n "$ENTITIES_DS_ID" ]]; then
  patch_relation_prop "$SOURCES_DS_ID" "Related Entities" "$ENTITIES_DS_ID" "single"
  patch_relation_prop "$WIKI_DS_ID" "Related Entities" "$ENTITIES_DS_ID" "single"
fi

# ---------------------------
# Seed rows dynamically
# ---------------------------

# Policy seed
POLICY_SEED="$TMP_DIR/policy_seed_props.json"
json_init_object "$POLICY_SEED"
add_prop "$POLICY_SEED" "Policy Name" '{ "title": [{ "text": { "content": "Default Global Policy" } }] }'
add_prop "$POLICY_SEED" "Policy Version" '{ "rich_text": [{ "text": { "content": "v1" } }] }'
add_prop "$POLICY_SEED" "Policy Scope" '{ "select": { "name": "global" } }'
add_prop "$POLICY_SEED" "Active" '{ "checkbox": true }'

if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
  add_prop "$POLICY_SEED" "Prompt Bundle Pointer" "$(jq -cn --arg v "$POLICY_PROMPT_BUNDLE_URL" '{url:$v}')"
  add_prop "$POLICY_SEED" "Citation Policy Pointer" "$(jq -cn --arg v "$POLICY_CITATION_URL" '{url:$v}')"
  add_prop "$POLICY_SEED" "Page Template Pointer" "$(jq -cn --arg v "$POLICY_TEMPLATE_URL" '{url:$v}')"
  add_prop "$POLICY_SEED" "Max Source Count" '{ "number": 25 }'
fi

if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
  add_prop "$POLICY_SEED" "Auto Publish Allowed" '{ "checkbox": false }'
  add_prop "$POLICY_SEED" "Requires Human Review" '{ "checkbox": true }'
fi

if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
  add_prop "$POLICY_SEED" "Regeneration Threshold Days" '{ "number": 14 }'
fi

if [[ "$FEATURE_CONFIDENCE" == "1" ]]; then
  add_prop "$POLICY_SEED" "Conflict Resolution Pointer" "$(jq -cn --arg v "$POLICY_CONFLICT_URL" '{url:$v}')"
fi

if [[ "$FEATURE_SOURCE_ENRICHMENT" == "1" ]]; then
  add_prop "$POLICY_SEED" "Updated At" '{ "date": { "start": "2026-04-09" } }'
  add_prop "$POLICY_SEED" "Notes" '{ "rich_text": [{ "text": { "content": "Default operating policy for initial deployment." } }] }'
fi

POLICY_PAGE_PAYLOAD="$TMP_DIR/policy_page.json"
create_page_payload "$POLICY_PAGE_PAYLOAD" "$POLICIES_DS_ID" "$POLICY_SEED"
POLICY_PAGE_RESP="$(api POST /pages "$POLICY_PAGE_PAYLOAD")"
POLICY_PAGE_ID="$(jq -r '.id' <<<"$POLICY_PAGE_RESP")"

# Source seed
SOURCE_SEED="$TMP_DIR/source_seed_props.json"
json_init_object "$SOURCE_SEED"
add_prop "$SOURCE_SEED" "Source Title" '{ "title": [{ "text": { "content": "Karpathy llm-wiki gist" } }] }'
add_prop "$SOURCE_SEED" "Source ID" '{ "rich_text": [{ "text": { "content": "src_karpathy_llmwiki_001" } }] }'
add_prop "$SOURCE_SEED" "Source Type" '{ "select": { "name": "web_page" } }'
add_prop "$SOURCE_SEED" "Canonical URL" '{ "url": "https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f" }'
add_prop "$SOURCE_SEED" "Trust Level" '{ "select": { "name": "primary" } }'
add_prop "$SOURCE_SEED" "Source Status" '{ "select": { "name": "queued" } }'
add_prop "$SOURCE_SEED" "Imported At" '{ "date": { "start": "2026-04-09T14:00:00Z" } }'

if [[ "$FEATURE_SOURCE_ENRICHMENT" == "1" ]]; then
  add_prop "$SOURCE_SEED" "Content Version" '{ "number": 1 }'
  add_prop "$SOURCE_SEED" "Freshness SLA Days" '{ "number": 30 }'
  add_prop "$SOURCE_SEED" "Last Seen At" '{ "date": { "start": "2026-04-09T14:00:00Z" } }'
fi

if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
  add_prop "$SOURCE_SEED" "Review Required" '{ "checkbox": false }'
fi

if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
  add_prop "$SOURCE_SEED" "Trigger Regeneration" '{ "checkbox": true }'
fi

SOURCE_PAGE_PAYLOAD="$TMP_DIR/source_page.json"
create_page_payload "$SOURCE_PAGE_PAYLOAD" "$SOURCES_DS_ID" "$SOURCE_SEED"
SOURCE_PAGE_RESP="$(api POST /pages "$SOURCE_PAGE_PAYLOAD")"
SOURCE_PAGE_ID="$(jq -r '.id' <<<"$SOURCE_PAGE_RESP")"

# Job seed
JOB_SEED="$TMP_DIR/job_seed_props.json"
json_init_object "$JOB_SEED"
add_prop "$JOB_SEED" "Job Title" '{ "title": [{ "text": { "content": "Ingest Karpathy llm-wiki gist" } }] }'
add_prop "$JOB_SEED" "Job ID" '{ "rich_text": [{ "text": { "content": "job_000001" } }] }'
add_prop "$JOB_SEED" "Job Type" '{ "select": { "name": "ingest_source" } }'
add_prop "$JOB_SEED" "Job Status" '{ "select": { "name": "queued" } }'
add_prop "$JOB_SEED" "Queue Timestamp" '{ "date": { "start": "2026-04-09T14:05:00Z" } }'

if [[ "$FEATURE_JOB_CONTROL" == "1" ]]; then
  add_prop "$JOB_SEED" "Trigger Type" '{ "select": { "name": "manual" } }'
  add_prop "$JOB_SEED" "Priority" '{ "select": { "name": "high" } }'
  add_prop "$JOB_SEED" "Attempt Count" '{ "number": 0 }'
  add_prop "$JOB_SEED" "Max Attempts" '{ "number": 8 }'
  add_prop "$JOB_SEED" "Worker Name" '{ "rich_text": [{ "text": { "content": "bootstrap" } }] }'
  add_prop "$JOB_SEED" "Idempotency Key" '{ "rich_text": [{ "text": { "content": "bootstrap:src_karpathy_llmwiki_001:ingest:v1" } }] }'
  add_prop "$JOB_SEED" "Locked" '{ "checkbox": false }'
fi

if [[ "$FEATURE_LINK_GRAPH" == "1" ]]; then
  add_prop "$JOB_SEED" "Target Source" "$(jq -cn --arg id "$SOURCE_PAGE_ID" '{relation:[{id:$id}]}')"
fi

if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
  add_prop "$JOB_SEED" "Policy Version Ref" "$(jq -cn --arg id "$POLICY_PAGE_ID" '{relation:[{id:$id}]}')"
fi

JOB_PAGE_PAYLOAD="$TMP_DIR/job_page.json"
create_page_payload "$JOB_PAGE_PAYLOAD" "$JOBS_DS_ID" "$JOB_SEED"
JOB_PAGE_RESP="$(api POST /pages "$JOB_PAGE_PAYLOAD")"
JOB_PAGE_ID="$(jq -r '.id' <<<"$JOB_PAGE_RESP")"

# ---------------------------
# Output
# ---------------------------

echo "Bootstrap complete."
echo
echo "Flags:"
echo "  FEATURE_SOURCE_ENRICHMENT=$FEATURE_SOURCE_ENRICHMENT"
echo "  FEATURE_SOURCE_DIAGNOSTICS=$FEATURE_SOURCE_DIAGNOSTICS"
echo "  FEATURE_EDITORIAL_WORKFLOW=$FEATURE_EDITORIAL_WORKFLOW"
echo "  FEATURE_FRESHNESS=$FEATURE_FRESHNESS"
echo "  FEATURE_CONFIDENCE=$FEATURE_CONFIDENCE"
echo "  FEATURE_LINK_GRAPH=$FEATURE_LINK_GRAPH"
echo "  FEATURE_ENTITIES=$FEATURE_ENTITIES"
echo "  FEATURE_QUESTIONS=$FEATURE_QUESTIONS"
echo "  FEATURE_JOB_CONTROL=$FEATURE_JOB_CONTROL"
echo "  FEATURE_POLICY_ENGINE=$FEATURE_POLICY_ENGINE"
echo
echo "Exports:"
echo "export CONTROL_DB_ID=$CONTROL_DB_ID"
echo "export SOURCES_DS_ID=$SOURCES_DS_ID"
echo "export WIKI_DS_ID=$WIKI_DS_ID"
echo "export JOBS_DS_ID=$JOBS_DS_ID"
echo "export POLICIES_DS_ID=$POLICIES_DS_ID"
[[ -n "$ENTITIES_DS_ID" ]] && echo "export ENTITIES_DS_ID=$ENTITIES_DS_ID"
[[ -n "$QUESTIONS_DS_ID" ]] && echo "export QUESTIONS_DS_ID=$QUESTIONS_DS_ID"
echo "export POLICY_PAGE_ID=$POLICY_PAGE_ID"
echo "export SOURCE_PAGE_ID=$SOURCE_PAGE_ID"
echo "export JOB_PAGE_ID=$JOB_PAGE_ID"