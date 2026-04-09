#!/usr/bin/env bash
set -euo pipefail

# llmwiki_notion_setup.sh
#
# Interactive entry point for:
#   1. selecting feature flags
#   2. collecting required inputs
#   3. running bootstrap
#   4. running verification
#
# Assumes these files exist in the same directory:
#   - bootstrap_llmwiki_notion_dynamic.sh
#   - verify_llmwiki_notion_dynamic.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOOTSTRAP_SCRIPT="${SCRIPT_DIR}/bootstrap_llmwiki_notion_dynamic.sh"
VERIFY_SCRIPT="${SCRIPT_DIR}/verify_llmwiki_notion_dynamic.sh"

require_file() {
  [[ -f "$1" ]] || {
    echo "Missing required file: $1" >&2
    exit 1
  }
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

require_file "$BOOTSTRAP_SCRIPT"
require_file "$VERIFY_SCRIPT"
require_cmd bash
require_cmd mktemp
require_cmd grep
require_cmd sed

prompt_yes_no() {
  local prompt="$1"
  local default="${2:-1}"
  local suffix
  local reply

  if [[ "$default" == "1" ]]; then
    suffix="[Y/n]"
  else
    suffix="[y/N]"
  fi

  while true; do
    read -r -p "$prompt $suffix " reply || true
    reply="${reply:-}"
    case "${reply,,}" in
      "")
        echo "$default"
        return
        ;;
      y|yes)
        echo "1"
        return
        ;;
      n|no)
        echo "0"
        return
        ;;
      *)
        echo "Please answer y or n." >&2
        ;;
    esac
  done
}

prompt_required() {
  local var_name="$1"
  local prompt="$2"
  local secret="${3:-0}"
  local value=""

  while [[ -z "$value" ]]; do
    if [[ "$secret" == "1" ]]; then
      read -r -s -p "$prompt: " value || true
      echo
    else
      read -r -p "$prompt: " value || true
    fi
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"
    [[ -n "$value" ]] || echo "This value is required." >&2
  done

  printf -v "$var_name" '%s' "$value"
}

prompt_optional() {
  local var_name="$1"
  local prompt="$2"
  local default="${3:-}"
  local secret="${4:-0}"
  local value=""

  if [[ "$secret" == "1" ]]; then
    read -r -s -p "$prompt${default:+ [$default]}: " value || true
    echo
  else
    read -r -p "$prompt${default:+ [$default]}: " value || true
  fi

  value="${value:-$default}"
  printf -v "$var_name" '%s' "$value"
}

echo "LLMWiki Notion setup"
echo

echo "Feature selection"
FEATURE_SOURCE_ENRICHMENT="$(prompt_yes_no "Enable source enrichment?" 1)"
FEATURE_SOURCE_DIAGNOSTICS="$(prompt_yes_no "Enable source diagnostics?" 1)"
FEATURE_EDITORIAL_WORKFLOW="$(prompt_yes_no "Enable editorial workflow?" 1)"
FEATURE_FRESHNESS="$(prompt_yes_no "Enable freshness management?" 1)"
FEATURE_CONFIDENCE="$(prompt_yes_no "Enable confidence/conflict fields?" 0)"
FEATURE_LINK_GRAPH="$(prompt_yes_no "Enable link graph relations?" 1)"
FEATURE_ENTITIES="$(prompt_yes_no "Enable entities data source?" 0)"
FEATURE_QUESTIONS="$(prompt_yes_no "Enable questions data source?" 0)"
FEATURE_JOB_CONTROL="$(prompt_yes_no "Enable advanced job control?" 1)"
FEATURE_POLICY_ENGINE="$(prompt_yes_no "Enable policy engine fields?" 1)"

echo
echo "Required inputs"
prompt_required NOTION_TOKEN "Notion integration token" 1
prompt_required PARENT_PAGE_ID "Parent page ID"

echo
echo "Optional inputs"
prompt_optional NOTION_VERSION "Notion API version" "2026-03-11"
prompt_optional API_BASE "Notion API base URL" "https://api.notion.com/v1"

if [[ "$FEATURE_POLICY_ENGINE" == "1" ]]; then
  echo
  echo "Optional policy pointer URLs"
  prompt_optional POLICY_PROMPT_BUNDLE_URL "Prompt bundle URL" "https://example.com/policies/prompt-bundle-v1.md"
  prompt_optional POLICY_CITATION_URL "Citation policy URL" "https://example.com/policies/citation-policy-v1.md"
  prompt_optional POLICY_TEMPLATE_URL "Page template URL" "https://example.com/policies/page-template-v1.md"
  if [[ "$FEATURE_CONFIDENCE" == "1" ]]; then
    prompt_optional POLICY_CONFLICT_URL "Conflict resolution URL" "https://example.com/policies/conflict-resolution-v1.md"
  else
    POLICY_CONFLICT_URL=""
  fi
else
  POLICY_PROMPT_BUNDLE_URL=""
  POLICY_CITATION_URL=""
  POLICY_TEMPLATE_URL=""
  POLICY_CONFLICT_URL=""
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

BOOTSTRAP_OUT="$TMP_DIR/bootstrap.out"

echo
echo "Running bootstrap..."
env \
  NOTION_TOKEN="$NOTION_TOKEN" \
  PARENT_PAGE_ID="$PARENT_PAGE_ID" \
  NOTION_VERSION="$NOTION_VERSION" \
  API_BASE="$API_BASE" \
  FEATURE_SOURCE_ENRICHMENT="$FEATURE_SOURCE_ENRICHMENT" \
  FEATURE_SOURCE_DIAGNOSTICS="$FEATURE_SOURCE_DIAGNOSTICS" \
  FEATURE_EDITORIAL_WORKFLOW="$FEATURE_EDITORIAL_WORKFLOW" \
  FEATURE_FRESHNESS="$FEATURE_FRESHNESS" \
  FEATURE_CONFIDENCE="$FEATURE_CONFIDENCE" \
  FEATURE_LINK_GRAPH="$FEATURE_LINK_GRAPH" \
  FEATURE_ENTITIES="$FEATURE_ENTITIES" \
  FEATURE_QUESTIONS="$FEATURE_QUESTIONS" \
  FEATURE_JOB_CONTROL="$FEATURE_JOB_CONTROL" \
  FEATURE_POLICY_ENGINE="$FEATURE_POLICY_ENGINE" \
  POLICY_PROMPT_BUNDLE_URL="$POLICY_PROMPT_BUNDLE_URL" \
  POLICY_CITATION_URL="$POLICY_CITATION_URL" \
  POLICY_TEMPLATE_URL="$POLICY_TEMPLATE_URL" \
  POLICY_CONFLICT_URL="$POLICY_CONFLICT_URL" \
  bash "$BOOTSTRAP_SCRIPT" | tee "$BOOTSTRAP_OUT"

extract_export() {
  local name="$1"
  local file="$2"
  local line value

  line="$(grep -E "^export ${name}=" "$file" | tail -n1 || true)"
  [[ -n "$line" ]] || return 1
  value="${line#export ${name}=}"
  printf '%s' "$value"
}

CONTROL_DB_ID="$(extract_export CONTROL_DB_ID "$BOOTSTRAP_OUT")"
SOURCES_DS_ID="$(extract_export SOURCES_DS_ID "$BOOTSTRAP_OUT")"
WIKI_DS_ID="$(extract_export WIKI_DS_ID "$BOOTSTRAP_OUT")"
JOBS_DS_ID="$(extract_export JOBS_DS_ID "$BOOTSTRAP_OUT")"
POLICIES_DS_ID="$(extract_export POLICIES_DS_ID "$BOOTSTRAP_OUT")"
POLICY_PAGE_ID="$(extract_export POLICY_PAGE_ID "$BOOTSTRAP_OUT" || true)"
SOURCE_PAGE_ID="$(extract_export SOURCE_PAGE_ID "$BOOTSTRAP_OUT" || true)"
JOB_PAGE_ID="$(extract_export JOB_PAGE_ID "$BOOTSTRAP_OUT" || true)"
ENTITIES_DS_ID="$(extract_export ENTITIES_DS_ID "$BOOTSTRAP_OUT" || true)"
QUESTIONS_DS_ID="$(extract_export QUESTIONS_DS_ID "$BOOTSTRAP_OUT" || true)"

for required in CONTROL_DB_ID SOURCES_DS_ID WIKI_DS_ID JOBS_DS_ID POLICIES_DS_ID; do
  if [[ -z "${!required:-}" ]]; then
    echo "Failed to parse required bootstrap output: $required" >&2
    exit 1
  fi
done

echo
echo "Running verification..."
env \
  NOTION_TOKEN="$NOTION_TOKEN" \
  NOTION_VERSION="$NOTION_VERSION" \
  API_BASE="$API_BASE" \
  CONTROL_DB_ID="$CONTROL_DB_ID" \
  SOURCES_DS_ID="$SOURCES_DS_ID" \
  WIKI_DS_ID="$WIKI_DS_ID" \
  JOBS_DS_ID="$JOBS_DS_ID" \
  POLICIES_DS_ID="$POLICIES_DS_ID" \
  POLICY_PAGE_ID="$POLICY_PAGE_ID" \
  SOURCE_PAGE_ID="$SOURCE_PAGE_ID" \
  JOB_PAGE_ID="$JOB_PAGE_ID" \
  ENTITIES_DS_ID="$ENTITIES_DS_ID" \
  QUESTIONS_DS_ID="$QUESTIONS_DS_ID" \
  FEATURE_SOURCE_ENRICHMENT="$FEATURE_SOURCE_ENRICHMENT" \
  FEATURE_SOURCE_DIAGNOSTICS="$FEATURE_SOURCE_DIAGNOSTICS" \
  FEATURE_EDITORIAL_WORKFLOW="$FEATURE_EDITORIAL_WORKFLOW" \
  FEATURE_FRESHNESS="$FEATURE_FRESHNESS" \
  FEATURE_CONFIDENCE="$FEATURE_CONFIDENCE" \
  FEATURE_LINK_GRAPH="$FEATURE_LINK_GRAPH" \
  FEATURE_ENTITIES="$FEATURE_ENTITIES" \
  FEATURE_QUESTIONS="$FEATURE_QUESTIONS" \
  FEATURE_JOB_CONTROL="$FEATURE_JOB_CONTROL" \
  FEATURE_POLICY_ENGINE="$FEATURE_POLICY_ENGINE" \
  bash "$VERIFY_SCRIPT"

echo
echo "Done."
echo
echo "Useful exports:"
echo "export CONTROL_DB_ID=$CONTROL_DB_ID"
echo "export SOURCES_DS_ID=$SOURCES_DS_ID"
echo "export WIKI_DS_ID=$WIKI_DS_ID"
echo "export JOBS_DS_ID=$JOBS_DS_ID"
echo "export POLICIES_DS_ID=$POLICIES_DS_ID"
[[ -n "$ENTITIES_DS_ID" ]] && echo "export ENTITIES_DS_ID=$ENTITIES_DS_ID"
[[ -n "$QUESTIONS_DS_ID" ]] && echo "export QUESTIONS_DS_ID=$QUESTIONS_DS_ID"
[[ -n "$POLICY_PAGE_ID" ]] && echo "export POLICY_PAGE_ID=$POLICY_PAGE_ID"
[[ -n "$SOURCE_PAGE_ID" ]] && echo "export SOURCE_PAGE_ID=$SOURCE_PAGE_ID"
[[ -n "$JOB_PAGE_ID" ]] && echo "export JOB_PAGE_ID=$JOB_PAGE_ID"