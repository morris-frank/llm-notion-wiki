#!/usr/bin/env bash
set -euo pipefail

# verify_llmwiki_notion_dynamic.sh
#
# Verifies a dynamically bootstrapped Notion LLMWiki control plane.
# This version is feature-flag aware: it only checks for properties,
# data sources, relations, seed fields, and smoke-test queries that
# should exist for the enabled flag set.
#
# Required env:
#   NOTION_TOKEN
#   CONTROL_DB_ID
#   SOURCES_DS_ID
#   WIKI_DS_ID
#   JOBS_DS_ID
#   POLICIES_DS_ID
#
# Optional env:
#   NOTION_VERSION=2026-03-11
#   API_BASE=https://api.notion.com/v1
#   POLICY_PAGE_ID
#   SOURCE_PAGE_ID
#   JOB_PAGE_ID
#   ENTITIES_DS_ID
#   QUESTIONS_DS_ID
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

: "${NOTION_TOKEN:?Missing NOTION_TOKEN}"
: "${CONTROL_DB_ID:?Missing CONTROL_DB_ID}"
: "${SOURCES_DS_ID:?Missing SOURCES_DS_ID}"
: "${WIKI_DS_ID:?Missing WIKI_DS_ID}"
: "${JOBS_DS_ID:?Missing JOBS_DS_ID}"
: "${POLICIES_DS_ID:?Missing POLICIES_DS_ID}"

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

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

require_cmd curl
require_cmd jq
require_cmd mktemp

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

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

json_tmp() {
  local file="$1"
  cat >"$file"
}

pass() { echo "PASS: $*"; }
warn() { echo "WARN: $*" >&2; }
fail() { echo "FAIL: $*" >&2; exit 1; }

assert_eq() {
  local got="$1"
  local want="$2"
  local msg="$3"
  [[ "$got" == "$want" ]] || fail "$msg (got=$got want=$want)"
  pass "$msg"
}

assert_nonempty() {
  local val="$1"
  local msg="$2"
  [[ -n "$val" && "$val" != "null" ]] || fail "$msg"
  pass "$msg"
}

assert_prop_present() {
  local json="$1"
  local prop="$2"
  jq -e --arg p "$prop" '.properties[$p]' >/dev/null <<<"$json" || fail "Missing property '$prop'"
  pass "Property present: $prop"
}

assert_prop_absent() {
  local json="$1"
  local prop="$2"
  if jq -e --arg p "$prop" '.properties[$p]' >/dev/null <<<"$json"; then
    fail "Property should be absent but is present: '$prop'"
  fi
  pass "Property absent as expected: $prop"
}

assert_prop_type() {
  local json="$1"
  local prop="$2"
  local want="$3"
  local got
  got="$(jq -r --arg p "$prop" '.properties[$p].type' <<<"$json")"
  assert_eq "$got" "$want" "Property type for '$prop'"
}

assert_page_exists() {
  local page_id="$1"
  local label="$2"
  local resp
  resp="$(api GET "/pages/${page_id}")" || fail "Could not retrieve ${label} page ${page_id}"
  local got_id
  got_id="$(jq -r '.id' <<<"$resp")"
  assert_nonempty "$got_id" "${label} page exists"
}

check_select_option_present() {
  local json="$1"
  local prop="$2"
  local option="$3"
  jq -e --arg p "$prop" --arg o "$option" \
    '.properties[$p].select.options[]? | select(.name == $o)' >/dev/null <<<"$json" \
    || fail "Missing select option '$option' on '$prop'"
  pass "Select option present: $prop -> $option"
}

check_select_option_absent() {
  local json="$1"
  local prop="$2"
  local option="$3"
  if jq -e --arg p "$prop" --arg o "$option" \
    '.properties[$p].select.options[]? | select(.name == $o)' >/dev/null <<<"$json"; then
    fail "Select option '$option' on '$prop' should be absent"
  fi
  pass "Select option absent as expected: $prop -> $option"
}

relation_target_id() {
  local json="$1"
  local prop="$2"
  jq -r --arg p "$prop" '
    .properties[$p].relation.data_source_id //
    .properties[$p].relation.database_id //
    empty
  ' <<<"$json"
}

assert_relation_target() {
  local json="$1"
  local prop="$2"
  local target="$3"

  local got
  got="$(relation_target_id "$json" "$prop")"
  [[ -n "$got" ]] || fail "Could not read relation target for '$prop'"
  assert_eq "$got" "$target" "Relation target for '$prop'"
}

assert_page_has_prop() {
  local json="$1"
  local prop="$2"
  jq -e --arg p "$prop" '.properties[$p]' >/dev/null <<<"$json" || fail "Page missing property '$prop'"
  pass "Seed page has property: $prop"
}

echo "# Retrieving database"
DB_JSON="$(api GET "/databases/${CONTROL_DB_ID}")"
DB_ID="$(jq -r '.id' <<<"$DB_JSON")"
assert_eq "$DB_ID" "$CONTROL_DB_ID" "Database reachable"

echo
echo "# Retrieving required data sources"
SOURCES_JSON="$(api GET "/data_sources/${SOURCES_DS_ID}")"
WIKI_JSON="$(api GET "/data_sources/${WIKI_DS_ID}")"
JOBS_JSON="$(api GET "/data_sources/${JOBS_DS_ID}")"
POLICIES_JSON="$(api GET "/data_sources/${POLICIES_DS_ID}")"

assert_eq "$(jq -r '.id' <<<"$SOURCES_JSON")" "$SOURCES_DS_ID" "Sources data source reachable"
assert_eq "$(jq -r '.id' <<<"$WIKI_JSON")" "$WIKI_DS_ID" "Wiki Pages data source reachable"
assert_eq "$(jq -r '.id' <<<"$JOBS_JSON")" "$JOBS_DS_ID" "Jobs data source reachable"
assert_eq "$(jq -r '.id' <<<"$POLICIES_JSON")" "$POLICIES_DS_ID" "Policies data source reachable"

echo
echo "# Retrieving optional data sources when enabled"
ENTITIES_JSON=""
QUESTIONS_JSON=""

if [[ "$FEATURE_ENTITIES" == "1" ]]; then
  : "${ENTITIES_DS_ID:?FEATURE_ENTITIES=1 but ENTITIES_DS_ID is missing}"
  ENTITIES_JSON="$(api GET "/data_sources/${ENTITIES_DS_ID}")"
  assert_eq "$(jq -r '.id' <<<"$ENTITIES_JSON")" "$ENTITIES_DS_ID" "Entities data source reachable"
else
  [[ -z "${ENTITIES_DS_ID:-}" ]] || warn "ENTITIES_DS_ID is set while FEATURE_ENTITIES=0"
fi

if [[ "$FEATURE_QUESTIONS" == "1" ]]; then
  : "${QUESTIONS_DS_ID:?FEATURE_QUESTIONS=1 but QUESTIONS_DS_ID is missing}"
  QUESTIONS_JSON="$(api GET "/data_sources/${QUESTIONS_DS_ID}")"
  assert_eq "$(jq -r '.id' <<<"$QUESTIONS_JSON")" "$QUESTIONS_DS_ID" "Questions data source reachable"
else
  [[ -z "${QUESTIONS_DS_ID:-}" ]] || warn "QUESTIONS_DS_ID is set while FEATURE_QUESTIONS=0"
fi

echo
echo "# Verifying Sources schema"

# always-on
for p in \
  "Source Title" "Source ID" "Scope" "Owner" "Source Type" "Canonical URL" \
  "Trust Level" "Source Status" "Imported At"
do
  assert_prop_present "$SOURCES_JSON" "$p"
done

check_select_option_present "$SOURCES_JSON" "Scope" "shared"
check_select_option_present "$SOURCES_JSON" "Scope" "private"

# optional present
if [[ "$FEATURE_SOURCE_ENRICHMENT" == "1" ]]; then
  for p in \
    "External File Key" "Source Checksum" "Project" "Topic Tags" \
    "Language" "Last Seen At" "Content Version" "Freshness SLA Days"
  do
    assert_prop_present "$SOURCES_JSON" "$p"
  done
else
  for p in \
    "External File Key" "Source Checksum" "Project" "Topic Tags" \
    "Language" "Last Seen At" "Content Version" "Freshness SLA Days"
  do
    assert_prop_absent "$SOURCES_JSON" "$p"
  done
fi

if [[ "$FEATURE_SOURCE_DIAGNOSTICS" == "1" ]]; then
  for p in \
    "Parse Error" "Last Error At" "Raw Text Pointer" \
    "Normalised Markdown Pointer" "Source Summary Pointer"
  do
    assert_prop_present "$SOURCES_JSON" "$p"
  done
else
  for p in \
    "Parse Error" "Last Error At" "Raw Text Pointer" \
    "Normalised Markdown Pointer" "Source Summary Pointer"
  do
    assert_prop_absent "$SOURCES_JSON" "$p"
  done
fi

if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
  assert_prop_present "$SOURCES_JSON" "Review Required"
else
  assert_prop_absent "$SOURCES_JSON" "Review Required"
fi

if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
  for p in "Last Parsed At" "Last Processed At" "Trigger Regeneration"; do
    assert_prop_present "$SOURCES_JSON" "$p"
  done
else
  for p in "Last Parsed At" "Last Processed At" "Trigger Regeneration"; do
    assert_prop_absent "$SOURCES_JSON" "$p"
  done
fi

if [[ "$FEATURE_LINK_GRAPH" == "1" ]]; then
  assert_prop_present "$SOURCES_JSON" "Related Wiki Pages"
  assert_prop_present "$SOURCES_JSON" "Latest Job"
else
  assert_prop_absent "$SOURCES_JSON" "Related Wiki Pages"
  assert_prop_absent "$SOURCES_JSON" "Latest Job"
fi

if [[ "$FEATURE_ENTITIES" == "1" ]]; then
  assert_prop_present "$SOURCES_JSON" "Related Entities"
else
  assert_prop_absent "$SOURCES_JSON" "Related Entities"
fi

echo
echo "# Verifying Wiki Pages schema"

for p in \
  "Wiki Title" "Wiki Slug" "Scope" "Owner" "Wiki Type" "Wiki Status" \
  "Canonical Markdown Path" "Summary" "Confidence Level"
do
  assert_prop_present "$WIKI_JSON" "$p"
done

check_select_option_present "$WIKI_JSON" "Scope" "shared"
check_select_option_present "$WIKI_JSON" "Scope" "private"

for option in "source" "concept" "synthesis" "index" "changelog"; do
  check_select_option_present "$WIKI_JSON" "Wiki Type" "$option"
done

if [[ "$FEATURE_SOURCE_ENRICHMENT" == "1" ]]; then
  assert_prop_present "$WIKI_JSON" "Published URL"
else
  assert_prop_absent "$WIKI_JSON" "Published URL"
fi

if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
  for p in \
    "Needs Human Review" "Review State" "Last Reviewed At" \
    "Last Published At" "Editorial State"
  do
    assert_prop_present "$WIKI_JSON" "$p"
  done
else
  for p in \
    "Needs Human Review" "Review State" "Last Reviewed At" \
    "Last Published At" "Editorial State"
  do
    assert_prop_absent "$WIKI_JSON" "$p"
  done
fi

if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
  for option in "unreviewed" "in_review" "approved" "rejected" "n_a"; do
    check_select_option_present "$WIKI_JSON" "Review State" "$option"
  done
fi

if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
  for p in "Last Generated At" "Regeneration Reason" "Freshness Target Days"; do
    assert_prop_present "$WIKI_JSON" "$p"
  done
  check_select_option_present "$WIKI_JSON" "Wiki Status" "stale"
else
  for p in "Last Generated At" "Regeneration Reason" "Freshness Target Days"; do
    assert_prop_absent "$WIKI_JSON" "$p"
  done
  check_select_option_absent "$WIKI_JSON" "Wiki Status" "stale"
fi

if [[ "$FEATURE_CONFIDENCE" == "1" ]]; then
  for p in "Confidence Score" "Conflict Flag"; do
    assert_prop_present "$WIKI_JSON" "$p"
  done
else
  for p in "Confidence Score" "Conflict Flag"; do
    assert_prop_absent "$WIKI_JSON" "$p"
  done
fi

if [[ "$FEATURE_LINK_GRAPH" == "1" ]]; then
  for p in "Source Count" "Link Count" "Backing Sources" "Latest Job"; do
    assert_prop_present "$WIKI_JSON" "$p"
  done
else
  for p in "Source Count" "Link Count" "Backing Sources" "Latest Job"; do
    assert_prop_absent "$WIKI_JSON" "$p"
  done
fi

if [[ "$FEATURE_ENTITIES" == "1" ]]; then
  assert_prop_present "$WIKI_JSON" "Related Entities"
else
  assert_prop_absent "$WIKI_JSON" "Related Entities"
fi

echo
echo "# Verifying Jobs schema"

for p in "Job Title" "Job ID" "Scope" "Owner" "Job Type" "Job Status" "Queue Timestamp"; do
  assert_prop_present "$JOBS_JSON" "$p"
done

check_select_option_present "$JOBS_JSON" "Scope" "shared"
check_select_option_present "$JOBS_JSON" "Scope" "private"

if [[ "$FEATURE_QUESTIONS" == "1" ]]; then
  check_select_option_present "$JOBS_JSON" "Job Type" "answer_question"
else
  check_select_option_absent "$JOBS_JSON" "Job Type" "answer_question"
fi

if [[ "$FEATURE_JOB_CONTROL" == "1" ]]; then
  for p in \
    "Trigger Type" "Trigger Event ID" "Priority" "Job Phase" "Attempt Count" \
    "Max Attempts" "Started At" "Finished At" "Duration Ms" \
    "Worker Name" "Idempotency Key" "Error Class" "Error Message" \
    "Retry After Seconds" "Output Pointer" "Diff Pointer" "Locked"
  do
    assert_prop_present "$JOBS_JSON" "$p"
  done
else
  for p in \
    "Trigger Type" "Trigger Event ID" "Priority" "Job Phase" "Attempt Count" \
    "Max Attempts" "Started At" "Finished At" "Duration Ms" \
    "Worker Name" "Idempotency Key" "Error Class" "Error Message" \
    "Retry After Seconds" "Output Pointer" "Diff Pointer" "Locked"
  do
    assert_prop_absent "$JOBS_JSON" "$p"
  done
fi

if [[ "$FEATURE_LINK_GRAPH" == "1" ]]; then
  assert_prop_present "$JOBS_JSON" "Target Source"
  assert_prop_present "$JOBS_JSON" "Target Wiki Page"
else
  assert_prop_absent "$JOBS_JSON" "Target Source"
  assert_prop_absent "$JOBS_JSON" "Target Wiki Page"
fi

if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
  assert_prop_present "$JOBS_JSON" "Policy Version Ref"
else
  assert_prop_absent "$JOBS_JSON" "Policy Version Ref"
fi

echo
echo "# Verifying Policies schema"

for p in "Policy Name" "Policy Version" "Policy Scope" "Active" "Policy Target Scope" "Policy Owner"; do
  assert_prop_present "$POLICIES_JSON" "$p"
done

for option in "all" "shared" "private"; do
  check_select_option_present "$POLICIES_JSON" "Policy Target Scope" "$option"
done

if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
  for p in \
    "Prompt Bundle Pointer" "Citation Policy Pointer" \
    "Page Template Pointer" "Max Source Count"
  do
    assert_prop_present "$POLICIES_JSON" "$p"
  done
else
  for p in \
    "Prompt Bundle Pointer" "Citation Policy Pointer" \
    "Page Template Pointer" "Max Source Count"
  do
    assert_prop_absent "$POLICIES_JSON" "$p"
  done
fi

if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
  for p in "Auto Publish Allowed" "Requires Human Review"; do
    assert_prop_present "$POLICIES_JSON" "$p"
  done
else
  for p in "Auto Publish Allowed" "Requires Human Review"; do
    assert_prop_absent "$POLICIES_JSON" "$p"
  done
fi

if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
  assert_prop_present "$POLICIES_JSON" "Regeneration Threshold Days"
else
  assert_prop_absent "$POLICIES_JSON" "Regeneration Threshold Days"
fi

if [[ "$FEATURE_CONFIDENCE" == "1" ]]; then
  assert_prop_present "$POLICIES_JSON" "Conflict Resolution Pointer"
else
  assert_prop_absent "$POLICIES_JSON" "Conflict Resolution Pointer"
fi

if [[ "$FEATURE_SOURCE_ENRICHMENT" == "1" ]]; then
  assert_prop_present "$POLICIES_JSON" "Updated At"
  assert_prop_present "$POLICIES_JSON" "Notes"
else
  assert_prop_absent "$POLICIES_JSON" "Updated At"
  assert_prop_absent "$POLICIES_JSON" "Notes"
fi

echo
echo "# Verifying optional data source schemas"

if [[ "$FEATURE_ENTITIES" == "1" ]]; then
  for p in "Entity Name" "Entity Type" "Canonical Entity ID"; do
    assert_prop_present "$ENTITIES_JSON" "$p"
  done
fi

if [[ "$FEATURE_QUESTIONS" == "1" ]]; then
  for p in "Question" "Question ID" "Question Status"; do
    assert_prop_present "$QUESTIONS_JSON" "$p"
  done
fi

echo
echo "# Verifying key property types"

assert_prop_type "$SOURCES_JSON" "Source Title" "title"
assert_prop_type "$SOURCES_JSON" "Source ID" "rich_text"
assert_prop_type "$SOURCES_JSON" "Scope" "select"
assert_prop_type "$SOURCES_JSON" "Owner" "rich_text"
assert_prop_type "$SOURCES_JSON" "Source Type" "select"
assert_prop_type "$SOURCES_JSON" "Canonical URL" "url"
assert_prop_type "$SOURCES_JSON" "Trust Level" "select"
assert_prop_type "$SOURCES_JSON" "Source Status" "select"
assert_prop_type "$SOURCES_JSON" "Imported At" "date"

assert_prop_type "$WIKI_JSON" "Wiki Title" "title"
assert_prop_type "$WIKI_JSON" "Wiki Slug" "rich_text"
assert_prop_type "$WIKI_JSON" "Scope" "select"
assert_prop_type "$WIKI_JSON" "Owner" "rich_text"
assert_prop_type "$WIKI_JSON" "Wiki Type" "select"
assert_prop_type "$WIKI_JSON" "Wiki Status" "select"
assert_prop_type "$WIKI_JSON" "Canonical Markdown Path" "rich_text"
assert_prop_type "$WIKI_JSON" "Summary" "rich_text"
assert_prop_type "$WIKI_JSON" "Confidence Level" "select"

assert_prop_type "$JOBS_JSON" "Job Title" "title"
assert_prop_type "$JOBS_JSON" "Job ID" "rich_text"
assert_prop_type "$JOBS_JSON" "Scope" "select"
assert_prop_type "$JOBS_JSON" "Owner" "rich_text"
assert_prop_type "$JOBS_JSON" "Job Type" "select"
assert_prop_type "$JOBS_JSON" "Job Status" "select"
assert_prop_type "$JOBS_JSON" "Queue Timestamp" "date"
if [[ "$FEATURE_JOB_CONTROL" == "1" ]]; then
  assert_prop_type "$JOBS_JSON" "Job Phase" "select"
fi

assert_prop_type "$POLICIES_JSON" "Policy Name" "title"
assert_prop_type "$POLICIES_JSON" "Policy Version" "rich_text"
assert_prop_type "$POLICIES_JSON" "Policy Scope" "select"
assert_prop_type "$POLICIES_JSON" "Active" "checkbox"
assert_prop_type "$POLICIES_JSON" "Policy Target Scope" "select"
assert_prop_type "$POLICIES_JSON" "Policy Owner" "rich_text"

echo
echo "# Verifying relation targets when enabled"

if [[ "$FEATURE_LINK_GRAPH" == "1" ]]; then
  assert_prop_type "$SOURCES_JSON" "Related Wiki Pages" "relation"
  assert_prop_type "$SOURCES_JSON" "Latest Job" "relation"
  assert_prop_type "$WIKI_JSON" "Backing Sources" "relation"
  assert_prop_type "$WIKI_JSON" "Latest Job" "relation"
  assert_prop_type "$JOBS_JSON" "Target Source" "relation"
  assert_prop_type "$JOBS_JSON" "Target Wiki Page" "relation"

  assert_relation_target "$SOURCES_JSON" "Related Wiki Pages" "$WIKI_DS_ID"
  assert_relation_target "$SOURCES_JSON" "Latest Job" "$JOBS_DS_ID"
  assert_relation_target "$WIKI_JSON" "Backing Sources" "$SOURCES_DS_ID"
  assert_relation_target "$WIKI_JSON" "Latest Job" "$JOBS_DS_ID"
  assert_relation_target "$JOBS_JSON" "Target Source" "$SOURCES_DS_ID"
  assert_relation_target "$JOBS_JSON" "Target Wiki Page" "$WIKI_DS_ID"
fi

if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
  assert_prop_type "$JOBS_JSON" "Policy Version Ref" "relation"
  assert_relation_target "$JOBS_JSON" "Policy Version Ref" "$POLICIES_DS_ID"
fi

if [[ "$FEATURE_ENTITIES" == "1" ]]; then
  assert_prop_type "$SOURCES_JSON" "Related Entities" "relation"
  assert_prop_type "$WIKI_JSON" "Related Entities" "relation"
  assert_relation_target "$SOURCES_JSON" "Related Entities" "$ENTITIES_DS_ID"
  assert_relation_target "$WIKI_JSON" "Related Entities" "$ENTITIES_DS_ID"
fi

echo
echo "# Optional seed page existence checks"

if [[ -n "${POLICY_PAGE_ID:-}" ]]; then
  assert_page_exists "$POLICY_PAGE_ID" "Policy seed"
else
  warn "POLICY_PAGE_ID not set, skipping policy seed existence check"
fi

if [[ -n "${SOURCE_PAGE_ID:-}" ]]; then
  assert_page_exists "$SOURCE_PAGE_ID" "Source seed"
else
  warn "SOURCE_PAGE_ID not set, skipping source seed existence check"
fi

if [[ -n "${JOB_PAGE_ID:-}" ]]; then
  assert_page_exists "$JOB_PAGE_ID" "Job seed"
else
  warn "JOB_PAGE_ID not set, skipping job seed existence check"
fi

echo
echo "# Optional seed page property checks"

if [[ -n "${POLICY_PAGE_ID:-}" ]]; then
  POLICY_PAGE_JSON="$(api GET "/pages/${POLICY_PAGE_ID}")"
  for p in "Policy Name" "Policy Version" "Policy Scope" "Active" "Policy Target Scope" "Policy Owner"; do
    assert_page_has_prop "$POLICY_PAGE_JSON" "$p"
  done
  if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
    for p in "Prompt Bundle Pointer" "Citation Policy Pointer" "Page Template Pointer" "Max Source Count"; do
      assert_page_has_prop "$POLICY_PAGE_JSON" "$p"
    done
  fi
fi

if [[ -n "${SOURCE_PAGE_ID:-}" ]]; then
  SOURCE_PAGE_JSON="$(api GET "/pages/${SOURCE_PAGE_ID}")"
  for p in "Source Title" "Source ID" "Scope" "Owner" "Source Type" "Canonical URL" "Trust Level" "Source Status" "Imported At"; do
    assert_page_has_prop "$SOURCE_PAGE_JSON" "$p"
  done
  if [[ "$FEATURE_SOURCE_ENRICHMENT" == "1" ]]; then
    for p in "Content Version" "Freshness SLA Days" "Last Seen At"; do
      assert_page_has_prop "$SOURCE_PAGE_JSON" "$p"
    done
  fi
  if [[ "$FEATURE_EDITORIAL_WORKFLOW" == "1" ]]; then
    assert_page_has_prop "$SOURCE_PAGE_JSON" "Review Required"
  fi
  if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
    assert_page_has_prop "$SOURCE_PAGE_JSON" "Trigger Regeneration"
  fi
fi

if [[ -n "${JOB_PAGE_ID:-}" ]]; then
  JOB_PAGE_JSON="$(api GET "/pages/${JOB_PAGE_ID}")"
  for p in "Job Title" "Job ID" "Scope" "Owner" "Job Type" "Job Status" "Queue Timestamp"; do
    assert_page_has_prop "$JOB_PAGE_JSON" "$p"
  done
  if [[ "$FEATURE_JOB_CONTROL" == "1" ]]; then
    for p in "Trigger Type" "Priority" "Job Phase" "Attempt Count" "Max Attempts" "Worker Name" "Idempotency Key" "Locked"; do
      assert_page_has_prop "$JOB_PAGE_JSON" "$p"
    done
  fi
  if [[ "$FEATURE_LINK_GRAPH" == "1" ]]; then
    assert_page_has_prop "$JOB_PAGE_JSON" "Target Source"
  fi
  if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
    assert_page_has_prop "$JOB_PAGE_JSON" "Policy Version Ref"
  fi
fi

echo
echo "# Smoke-test query: Sources"

SOURCES_QUERY="$TMP_DIR/query_sources.json"
if [[ "$FEATURE_FRESHNESS" == "1" ]]; then
  json_tmp "$SOURCES_QUERY" <<'JSON'
{
  "filter": {
    "or": [
      {
        "property": "Source Status",
        "select": { "equals": "queued" }
      },
      {
        "property": "Trigger Regeneration",
        "checkbox": { "equals": true }
      }
    ]
  },
  "sorts": [
    {
      "property": "Imported At",
      "direction": "ascending"
    }
  ],
  "page_size": 10
}
JSON
else
  json_tmp "$SOURCES_QUERY" <<'JSON'
{
  "filter": {
    "property": "Source Status",
    "select": { "equals": "queued" }
  },
  "sorts": [
    {
      "property": "Imported At",
      "direction": "ascending"
    }
  ],
  "page_size": 10
}
JSON
fi

SOURCES_QUERY_JSON="$(api POST "/data_sources/${SOURCES_DS_ID}/query" "$SOURCES_QUERY")"
SOURCES_COUNT="$(jq -r '.results | length' <<<"$SOURCES_QUERY_JSON")"
assert_nonempty "$SOURCES_COUNT" "Sources query returned a count"
pass "Sources query executed successfully (rows=${SOURCES_COUNT})"

echo "Top source rows:"
jq -r '
  .results[] |
  [
    .id,
    (.properties["Source Title"].title | map(.plain_text) | join("")),
    (.properties["Source Status"].select.name // ""),
    (.properties["Canonical URL"].url // "")
  ] | @tsv
' <<<"$SOURCES_QUERY_JSON" | sed $'s/\t/ | /g' || true

echo
echo "# Smoke-test query: Jobs"

JOBS_QUERY="$TMP_DIR/query_jobs.json"
if [[ "$FEATURE_JOB_CONTROL" == "1" ]]; then
  json_tmp "$JOBS_QUERY" <<'JSON'
{
  "filter": {
    "and": [
      {
        "property": "Job Status",
        "select": { "equals": "queued" }
      },
      {
        "property": "Locked",
        "checkbox": { "equals": false }
      }
    ]
  },
  "sorts": [
    {
      "property": "Queue Timestamp",
      "direction": "ascending"
    }
  ],
  "page_size": 10
}
JSON
else
  json_tmp "$JOBS_QUERY" <<'JSON'
{
  "filter": {
    "property": "Job Status",
    "select": { "equals": "queued" }
  },
  "sorts": [
    {
      "property": "Queue Timestamp",
      "direction": "ascending"
    }
  ],
  "page_size": 10
}
JSON
fi

JOBS_QUERY_JSON="$(api POST "/data_sources/${JOBS_DS_ID}/query" "$JOBS_QUERY")"
JOBS_COUNT="$(jq -r '.results | length' <<<"$JOBS_QUERY_JSON")"
assert_nonempty "$JOBS_COUNT" "Jobs query returned a count"
pass "Jobs query executed successfully (rows=${JOBS_COUNT})"

echo "Top job rows:"
jq -r '
  .results[] |
  [
    .id,
    (.properties["Job Title"].title | map(.plain_text) | join("")),
    (.properties["Job Type"].select.name // ""),
    (.properties["Job Status"].select.name // "")
  ] | @tsv
' <<<"$JOBS_QUERY_JSON" | sed $'s/\t/ | /g' || true

echo
echo "# Optional smoke-test query: Entities"
if [[ "$FEATURE_ENTITIES" == "1" ]]; then
  ENTITIES_QUERY="$TMP_DIR/query_entities.json"
  json_tmp "$ENTITIES_QUERY" <<'JSON'
{
  "page_size": 10
}
JSON
  ENTITIES_QUERY_JSON="$(api POST "/data_sources/${ENTITIES_DS_ID}/query" "$ENTITIES_QUERY")"
  ENTITIES_COUNT="$(jq -r '.results | length' <<<"$ENTITIES_QUERY_JSON")"
  assert_nonempty "$ENTITIES_COUNT" "Entities query returned a count"
  pass "Entities query executed successfully (rows=${ENTITIES_COUNT})"
fi

echo
echo "# Optional smoke-test query: Questions"
if [[ "$FEATURE_QUESTIONS" == "1" ]]; then
  QUESTIONS_QUERY="$TMP_DIR/query_questions.json"
  json_tmp "$QUESTIONS_QUERY" <<'JSON'
{
  "page_size": 10
}
JSON
  QUESTIONS_QUERY_JSON="$(api POST "/data_sources/${QUESTIONS_DS_ID}/query" "$QUESTIONS_QUERY")"
  QUESTIONS_COUNT="$(jq -r '.results | length' <<<"$QUESTIONS_QUERY_JSON")"
  assert_nonempty "$QUESTIONS_COUNT" "Questions query returned a count"
  pass "Questions query executed successfully (rows=${QUESTIONS_COUNT})"
fi

echo
echo "# Summary"
echo "Database:      $CONTROL_DB_ID"
echo "Sources DS:    $SOURCES_DS_ID"
echo "Wiki DS:       $WIKI_DS_ID"
echo "Jobs DS:       $JOBS_DS_ID"
echo "Policies DS:   $POLICIES_DS_ID"
[[ -n "${ENTITIES_DS_ID:-}" ]] && echo "Entities DS:   $ENTITIES_DS_ID"
[[ -n "${QUESTIONS_DS_ID:-}" ]] && echo "Questions DS:  $QUESTIONS_DS_ID"
echo "Feature flags:"
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
pass "Verification complete"
