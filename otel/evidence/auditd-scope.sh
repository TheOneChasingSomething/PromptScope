#!/usr/bin/env bash
# =============================================================================
# auditd-scope.sh — add/verify/remove a TARGETED auditd rule for one experiment.
#
# Independent system observation: records which programs were actually executed
# and which processes/threads were created by the experiment's account. This is
# the ENVIRONMENT-OBSERVES layer — complementary to session transcripts
# (agent-declares) and the tool executor.
#
# Run from the OPERATOR account (needs sudo for auditctl). Scope by the UID that
# actually runs opencode — ideally a dedicated, sudo-less account.
#
# What it does NOT give: stdout/stderr or the functional result of each command
# (use the tool executor), nor program termination (add an eBPF lifecycle probe
# such as BCC exitsnoop). 'always,exit' = process at syscall EXIT, not program
# termination; a successful execve does not mean the test then passed.
#
# Usage:
#   ./auditd-scope.sh add      [TARGET_USER]   # before opencode
#   ./auditd-scope.sh witness  [TARGET_USER]   # confirm a witness event is captured
#   ./auditd-scope.sh collect  OUT_DIR         # dump raw + interpreted, show losses
#   ./auditd-scope.sh remove                   # remove ONLY this rule (never -D)
#
# The rule key is derived per-UID and stored so add/remove/collect agree.
# =============================================================================
set -euo pipefail

ACTION="${1:?usage: add|witness|collect|remove [...]}"
KEYFILE="${AUDIT_KEYFILE:-$HOME/.opencode-audit-key}"
ARCH="${AUDIT_ARCH:-b64}"   # set b32 as a second rule for 32-bit coverage
SYSCALLS="execve,execveat,fork,vfork,clone,clone3"

resolve_uid() {
  local u="${1:-}"
  if [ -n "$u" ]; then id -u "$u"; else id -u; fi
}

case "$ACTION" in
  add)
    TARGET_UID="$(resolve_uid "${2:-}")"
    KEY="oc_$(date -u +%Y%m%dT%H%M%S)"
    printf '%s\n' "$KEY" > "$KEYFILE"
    echo "[audit] target uid=$TARGET_UID key=$KEY (stored in $KEYFILE)"
    # Verify the daemon is up and existing policy is not blocking first.
    sudo auditctl -s
    sudo auditctl -l | grep -q 'never,task' && \
      echo "[audit][warn] a 'never,task' rule is present; new-process auditing may be disabled" || true
    sudo auditctl -a always,exit -F "arch=$ARCH" -S "$SYSCALLS" -F uid="$TARGET_UID" -k "$KEY"
    echo "[audit] rule added. On x86-64, add a second rule with AUDIT_ARCH=b32 for 32-bit programs."
    ;;

  witness)
    TARGET_UID="$(resolve_uid "${2:-}")"
    [ -f "$KEYFILE" ] || { echo "[audit] no key file; run 'add' first" >&2; exit 1; }
    KEY="$(cat "$KEYFILE")"
    echo "[audit] run a witness command AS the target uid ($TARGET_UID), e.g.:"
    echo "        sudo -u \"#$TARGET_UID\" /usr/bin/printf 'OPENCODE_MONITOR_CHECK\\n'"
    echo "[audit] then confirm it was recorded:"
    echo "        sudo ausearch -k \"$KEY\" -i"
    echo "[audit] DO NOT start a campaign before seeing this witness event."
    ;;

  collect)
    OUT_DIR="${2:?usage: auditd-scope.sh collect OUT_DIR}"
    [ -f "$KEYFILE" ] || { echo "[audit] no key file; run 'add' first" >&2; exit 1; }
    KEY="$(cat "$KEYFILE")"
    mkdir -p "$OUT_DIR"
    sudo auditctl -s > "$OUT_DIR/audit-status-after.txt"
    sudo ausearch -k "$KEY" --raw > "$OUT_DIR/process-audit.raw" || true
    sudo ausearch -k "$KEY" -i    > "$OUT_DIR/process-audit.txt" || true
    echo "[audit] collected to $OUT_DIR"
    echo "[audit] compare the 'lost' counter before/after: an increase means kernel-queue records were dropped."
    echo "[audit] an unchanged counter does NOT prove the rules covered every relevant behaviour."
    ;;

  remove)
    [ -f "$KEYFILE" ] || { echo "[audit] no key file; nothing to remove" >&2; exit 0; }
    KEY="$(cat "$KEYFILE")"
    # Remove ONLY this rule — never 'auditctl -D', which wipes all rules.
    # Deletion must mirror the exact add specification.
    TARGET_UID="$(resolve_uid "${2:-}")"
    sudo auditctl -d always,exit -F "arch=$ARCH" -S "$SYSCALLS" -F uid="$TARGET_UID" -k "$KEY" \
      && echo "[audit] removed rule key=$KEY uid=$TARGET_UID" \
      || echo "[audit] rule not found (already removed?) — did NOT run auditctl -D"
    ;;

  *)
    echo "unknown action: $ACTION" >&2; exit 2 ;;
esac
