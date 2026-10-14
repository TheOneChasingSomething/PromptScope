#!/usr/bin/env bash
# =============================================================================
# run-experiment.sh — experiment supervisor for one opencode run.
#
# Implements the "base experimental archive" level of the evidence spec:
# an operator-controlled supervisor that mints a run_id BEFORE the agent starts,
# captures inputs/environment/events/sessions/artifacts into one per-run folder,
# and records an honest completeness report. It deliberately does NOT seal the
# folder (see seal-run.sh) and does NOT provide an independent system observer
# (see auditd-scope.sh) — those are separate, higher-assurance layers.
#
# Honest boundary:
#   - This captures what opencode and the working copy expose. It is a working
#     collection, not a tamper-proof archive: until seal-run.sh runs on a
#     separate archival host/account, an agent with the same OS rights could
#     alter it.
#   - What the agent DECLARES (session transcripts) is not the same as what the
#     ENVIRONMENT OBSERVED (auditd/executor). This script captures the former
#     plus harness logs; pair it with auditd-scope.sh for the latter.
#
# Usage:
#   ./run-experiment.sh "<task prompt>" [--agent NAME] [--workspace DIR]
#
# Environment:
#   EVIDENCE_ROOT       Where run folders are created (default: $HOME/evidence-staging).
#                       For real separation, point this at an operator-owned path
#                       the agent account cannot write.
#   OPENCODE_AGENT      Default agent/subcommand profile (default: build).
#   OPENCODE_SERVER_URL If set (e.g. http://127.0.0.1:4096), child sessions are
#                       enumerated via the V1 API GET /session/:id/children.
#   OPENCODE_CLI_GEN    Force "v1" or "v2" export syntax; otherwise auto-detected.
# =============================================================================
set -euo pipefail
umask 077

# --- arguments --------------------------------------------------------------
TASK_PROMPT="${1:-}"
[ -n "$TASK_PROMPT" ] || { echo "usage: $0 \"<task prompt>\" [--agent NAME] [--workspace DIR]" >&2; exit 2; }
shift || true

AGENT="${OPENCODE_AGENT:-build}"
WORKSPACE_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --agent)     AGENT="${2:?}"; shift 2 ;;
    --workspace) WORKSPACE_DIR="${2:?}"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

command -v opencode >/dev/null || { echo "opencode not found in PATH" >&2; exit 127; }

# --- run identity (minted by the supervisor, before the agent starts) -------
RUN_ID="run-$(date -u +%Y%m%d-%H%M%S)"
EVIDENCE_ROOT="${EVIDENCE_ROOT:-$HOME/evidence-staging}"
RUN_DIR="$EVIDENCE_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR"/{inputs/{instructions,skills,references},environment,events,sessions/children,artifacts/{tool-outputs,workspace-before,workspace-after,diagnostics},evaluation,telemetry}

echo "[supervisor] run_id=$RUN_ID"
echo "[supervisor] run_dir=$RUN_DIR"

# Track collection gaps honestly as we go.
MISSING_ITEMS=()
note_missing() { MISSING_ITEMS+=("$1"); echo "[supervisor][gap] $1" >&2; }

# --- 1. inputs --------------------------------------------------------------
printf '%s\n' "$TASK_PROMPT" > "$RUN_DIR/inputs/task.md"

# --- 2. environment ---------------------------------------------------------
{
  echo "{"
  echo "  \"captured_at_utc\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"opencode_version\": \"$(opencode --version 2>/dev/null | head -1 | sed 's/"/\\"/g')\","
  echo "  \"uname\": \"$(uname -a | sed 's/"/\\"/g')\","
  echo "  \"boot_id\": \"$(cat /proc/sys/kernel/random/boot_id 2>/dev/null || echo unknown)\","
  echo "  \"agent_profile\": \"$AGENT\""
  echo "}"
} > "$RUN_DIR/environment/versions.json"

# Resolved opencode config (best effort; model/permissions live here).
OC_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}/opencode/opencode.json"
if [ -f "$OC_CONFIG" ]; then
  cp "$OC_CONFIG" "$RUN_DIR/environment/configuration.json"
else
  note_missing "opencode configuration.json not found at $OC_CONFIG"
fi

# Source state of the working copy, if it is a git repo.
if git -C "." rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  {
    echo "{"
    echo "  \"commit\": \"$(git rev-parse HEAD 2>/dev/null)\","
    echo "  \"branch\": \"$(git rev-parse --abbrev-ref HEAD 2>/dev/null)\","
    echo "  \"dirty\": $(test -n "$(git status --porcelain 2>/dev/null)" && echo true || echo false)"
    echo "}"
  } > "$RUN_DIR/environment/source-state.json"
  git -C "." diff > "$RUN_DIR/environment/source-uncommitted.patch" 2>/dev/null || true
else
  note_missing "working directory is not a git repo; source-state.json omitted"
fi

# permissions.json is installation-specific; record that it must be filled.
echo '{"note":"fill with the resolved opencode permission set for this run"}' > "$RUN_DIR/environment/permissions.json"
note_missing "permissions.json is a placeholder — record the resolved permission set"

# Optional: snapshot the workspace BEFORE the run.
if [ -n "$WORKSPACE_DIR" ] && [ -d "$WORKSPACE_DIR" ]; then
  tar -C "$WORKSPACE_DIR" -czf "$RUN_DIR/artifacts/workspace-before/workspace.tar.gz" . 2>/dev/null \
    || note_missing "workspace-before snapshot failed"
fi

# --- 3. launch opencode under DEBUG, capturing streams separately -----------
# stdout -> structured events (JSON) ; stderr -> harness DEBUG log.
EXEC_STATUS="completed"
set +e
opencode --print-logs --log-level DEBUG \
  run --format json --agent "$AGENT" "$TASK_PROMPT" \
  > "$RUN_DIR/events/opencode.jsonl" \
  2> "$RUN_DIR/events/opencode-harness.log"
RC=$?
set -e
printf '%s\n' "$RC" > "$RUN_DIR/events/opencode-exit-code.txt"
if [ "$RC" -ne 0 ]; then
  EXEC_STATUS="interrupted"
  note_missing "opencode exited with code $RC (launch exit code is NOT the task verdict)"
fi

# --- 4. sessions: main + children (best effort) -----------------------------
# Extract the main session id from the structured event stream. Parsed with
# python for robustness (matches sessionID / session_id / sessionId, any case),
# and made abort-safe: a miss must not kill the run before completeness.json.
SESSION_ID="$(python3 - "$RUN_DIR/events/opencode.jsonl" <<'PY' 2>/dev/null || true
import json, re, sys
path = sys.argv[1]
want = re.compile(r'^session_?id$', re.I)
try:
    for line in open(path, encoding='utf-8', errors='replace'):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    if isinstance(v, str) and want.match(k):
                        print(v); sys.exit(0)
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)
except FileNotFoundError:
    pass
PY
)"

# Detect export syntax: V2 uses `opencode session export`, V1 uses `opencode export`.
CLI_GEN="${OPENCODE_CLI_GEN:-}"
if [ -z "$CLI_GEN" ]; then
  if opencode session --help >/dev/null 2>&1; then CLI_GEN="v2"; else CLI_GEN="v1"; fi
fi
echo "$CLI_GEN" > "$RUN_DIR/environment/opencode-cli-generation.txt"

export_session() { # <session_id> <dest_file>
  local sid="$1" dest="$2"
  if [ "$CLI_GEN" = "v2" ]; then
    opencode session export "$sid" > "$dest" 2>/dev/null
  else
    opencode export "$sid" > "$dest" 2>/dev/null
  fi
}

if [ -n "$SESSION_ID" ]; then
  echo "$SESSION_ID" > "$RUN_DIR/sessions/main-session-id.txt"
  export_session "$SESSION_ID" "$RUN_DIR/sessions/main.json" \
    || note_missing "export of main session $SESSION_ID failed"

  # Enumerate child sessions via the V1 API when a server URL is provided.
  if [ -n "${OPENCODE_SERVER_URL:-}" ]; then
    CH_JSON="$RUN_DIR/sessions/children/_index.json"
    if curl -fsS "${OPENCODE_SERVER_URL%/}/session/${SESSION_ID}/children" -o "$CH_JSON" 2>/dev/null; then
      # Export each child id found in the index.
      grep -oE '"id"[[:space:]]*:[[:space:]]*"[^"]+"' "$CH_JSON" | grep -oE '"[^"]+"$' | tr -d '"' \
        | while read -r cid; do
            [ -n "$cid" ] && export_session "$cid" "$RUN_DIR/sessions/children/$cid.json" || true
          done
    else
      note_missing "child session enumeration failed against $OPENCODE_SERVER_URL"
    fi
  else
    note_missing "child sessions not enumerated (set OPENCODE_SERVER_URL to enable)"
  fi
else
  note_missing "could not determine main session id from event stream — no sessions exported"
fi

# --- 5. workspace AFTER snapshot --------------------------------------------
if [ -n "$WORKSPACE_DIR" ] && [ -d "$WORKSPACE_DIR" ]; then
  tar -C "$WORKSPACE_DIR" -czf "$RUN_DIR/artifacts/workspace-after/workspace.tar.gz" . 2>/dev/null \
    || note_missing "workspace-after snapshot failed"
fi

# --- 6. completeness report (honest) ----------------------------------------
COLLECTION_STATUS="complete"
[ "${#MISSING_ITEMS[@]}" -eq 0 ] || COLLECTION_STATUS="partial"
{
  echo "{"
  echo "  \"run_id\": \"$RUN_ID\","
  echo "  \"execution_status\": \"$EXEC_STATUS\","
  echo "  \"task_result\": \"not_evaluated\","
  echo "  \"collection_status\": \"$COLLECTION_STATUS\","
  echo "  \"opencode_cli_generation\": \"$CLI_GEN\","
  printf '  "missing_items": ['
  if [ "${#MISSING_ITEMS[@]}" -gt 0 ]; then
    printf '\n'
    for i in "${!MISSING_ITEMS[@]}"; do
      sep=","; [ "$i" -eq $(( ${#MISSING_ITEMS[@]} - 1 )) ] && sep=""
      printf '    "%s"%s\n' "$(printf '%s' "${MISSING_ITEMS[$i]}" | sed 's/"/\\"/g')" "$sep"
    done
    printf '  '
  fi
  echo "]"
  echo "}"
} > "$RUN_DIR/completeness.json"

echo "[supervisor] collection_status=$COLLECTION_STATUS execution_status=$EXEC_STATUS"
echo "[supervisor] NOT sealed yet. Freeze + seal on the archival host: evidence/seal-run.sh \"$RUN_DIR\""
