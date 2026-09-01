#!/usr/bin/env bash
#
# check-figma-scopes.sh -- find out which Figma scopes a token actually has,
# and which of this proxy's tools will therefore work.
#
# Figma does not report granted scopes in its token response, but it DOES name
# them in 403 error messages:
#
#   Invalid scope: ["file_content:read"]. This endpoint requires the
#   file_read or files:read or file_metadata:read scope.
#
# The bracketed list is what the token HAS. This script calls one endpoint per
# tool and reads those messages back, so a scope problem is diagnosed in one
# run instead of inferred from tool failures one at a time.
#
# Usage:
#   export FIGMA_TOKEN='figd_...'            # PAT, or an OAuth access token
#   ./check-figma-scopes.sh <file_key> [team_id]
#
# The file key is in a Figma URL:
#   figma.com/design/ABC123xyz/My-Design  ->  ABC123xyz
# The team id is in a team URL:
#   figma.com/files/team/1234567890/My-Team  ->  1234567890
#
# NEVER pass the token as an argument -- it would land in your shell history.
# Use the environment variable.

set -uo pipefail

FILE_KEY="${1:-}"
TEAM_ID="${2:-}"
API="https://api.figma.com/v1"

if [[ -z "${FIGMA_TOKEN:-}" ]]; then
  echo "error: FIGMA_TOKEN is not set." >&2
  echo "  export FIGMA_TOKEN='figd_...'" >&2
  exit 1
fi

if [[ -z "$FILE_KEY" ]]; then
  echo "usage: FIGMA_TOKEN=... $0 <file_key> [team_id]" >&2
  exit 1
fi

# A PAT (figd_...) must go in X-Figma-Token; Figma explicitly rejects PATs in
# the Authorization header. An OAuth access token is the opposite. Pick the
# right header from the token's shape.
if [[ "$FIGMA_TOKEN" == figd_* ]]; then
  AUTH_HEADER="X-Figma-Token: $FIGMA_TOKEN"
  TOKEN_KIND="personal access token"
else
  AUTH_HEADER="Authorization: Bearer $FIGMA_TOKEN"
  TOKEN_KIND="OAuth access token"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

GRANTED=""
declare -a OK=() DENIED=() SKIPPED=() THROTTLED=()

# Figma rate limits per token. This script makes a dozen calls in a row, which
# is enough to trip it -- a small gap between probes avoids turning a scope
# check into a throttling test. Override with DELAY=0 if you are impatient.
DELAY="${DELAY:-1}"

probe () {                 # probe <tool-name> <required-scope> <path>
  local tool="$1" scope="$2" path="$3"
  local code
  code=$(curl -s -o "$TMP/r.json" -w "%{http_code}" --max-time 40 \
         -H "$AUTH_HEADER" "$API$path")

  if [[ "$code" == "200" ]]; then
    printf '  \033[32m%-4s\033[0m %-30s %s\n' "OK" "$tool" "$scope"
    OK+=("$tool")
    sleep "$DELAY"
    return
  fi

  # 429 is Figma throttling this token, NOT a permission problem. Flag it
  # separately -- treating it as a scope failure sends you looking in the
  # wrong place entirely.
  if [[ "$code" == "429" ]]; then
    printf '  \033[33m%-4s\033[0m %-30s %s\n' "429" "$tool" "$scope"
    printf '       rate limited -- inconclusive, not a scope problem. Retry in a few minutes.\n'
    THROTTLED+=("$tool")
    sleep 5
    return
  fi

  local msg
  msg=$(python3 -c "
import json,sys
try:    print(json.load(open('$TMP/r.json')).get('message','') or '')
except Exception: print('')
" 2>/dev/null)

  # Harvest the token's real scope list from the first message that reveals it.
  if [[ -z "$GRANTED" && "$msg" == *"Invalid scope"* ]]; then
    GRANTED=$(printf '%s' "$msg" | python3 -c "
import re,sys
s=sys.stdin.read()
m=re.search(r'Invalid scope\(?s?\)?:\s*\[?([^\]\.]+)\]?', s)
print(re.sub(r'[\"\x27]', '', m.group(1)).strip() if m else '')
" 2>/dev/null)
  fi

  printf '  \033[31m%-4s\033[0m %-30s %s\n' "$code" "$tool" "$scope"
  [[ -n "$msg" ]] && printf '       %s\n' "${msg:0:150}"
  DENIED+=("$tool")
  sleep "$DELAY"
}

echo
echo "Figma scope check"
echo "  token kind : $TOKEN_KIND"
echo "  file key   : $FILE_KEY"
[[ -n "$TEAM_ID" ]] && echo "  team id    : $TEAM_ID" || echo "  team id    : (not supplied -- team tools skipped)"
echo

echo "File-scoped tools"
probe "figma_get_me"              "current_user:read"        "/me"
probe "figma_get_file"            "file_content:read"        "/files/$FILE_KEY?depth=1"
probe "figma_get_file_metadata"   "file_metadata:read"       "/files/$FILE_KEY/meta"
probe "figma_get_file_comments"   "file_comments:read"       "/files/$FILE_KEY/comments"
probe "figma_get_file_versions"   "file_versions:read"       "/files/$FILE_KEY/versions"
probe "figma_get_dev_resources"   "file_dev_resources:read"  "/files/$FILE_KEY/dev_resources"
probe "figma_get_file_components" "library_content:read"     "/files/$FILE_KEY/components"
probe "figma_get_file_styles"     "library_content:read"     "/files/$FILE_KEY/styles"
probe "figma_get_file_variables"  "file_variables:read"      "/files/$FILE_KEY/variables/local"

echo
echo "Team-scoped tools"
if [[ -n "$TEAM_ID" ]]; then
  probe "figma_list_projects"        "folders:read"              "/teams/$TEAM_ID/projects"
  probe "figma_list_team_components" "team_library_content:read" "/teams/$TEAM_ID/components?page_size=1"
  probe "figma_list_team_styles"     "team_library_content:read" "/teams/$TEAM_ID/styles?page_size=1"
else
  echo "  -- skipped, pass a team_id as the second argument"
  SKIPPED+=("figma_list_projects" "figma_list_team_components" "figma_list_team_styles")
fi

echo
echo "Not probed (write operations -- would modify your Figma content)"
echo "  figma_post_comment          file_comments:write"
echo "  figma_create_dev_resource   file_dev_resources:write"
echo "  figma_delete_dev_resource   file_dev_resources:write"
echo "  figma_list_project_files    folders:read  (needs a project id from figma_list_projects)"

echo
echo "Summary"
echo "  working   : ${#OK[@]}"
echo "  denied    : ${#DENIED[@]}"
if [[ ${#THROTTLED[@]} -gt 0 ]]; then
  echo "  throttled : ${#THROTTLED[@]}  <- inconclusive, rerun later:"
  for t in "${THROTTLED[@]}"; do echo "                ${t}"; done
fi
if [[ -n "$GRANTED" ]]; then
  echo
  echo "  scopes this token actually holds, per Figma:"
  echo "    $GRANTED"
  echo
  echo "  Anything denied above is missing from that list. For an OAuth token"
  echo "  that means the Figma app version is not approved for the scope, or the"
  echo "  deployed figmaScopes string in cdk.json omits it."
else
  echo
  echo "  Figma did not reveal the token's scope list -- either everything"
  echo "  probed succeeded, or the failures were not scope-related."
fi

if [[ ${#DENIED[@]} -eq 0 && ${#THROTTLED[@]} -eq 0 && ${#OK[@]} -gt 0 ]]; then
  echo
  echo "  Every probed endpoint returned 200. A 403 from Quick after this is a"
  echo "  Quick-side problem -- most likely the tool is disabled in the"
  echo "  connector's permission settings rather than a Figma scope issue."
fi
echo
