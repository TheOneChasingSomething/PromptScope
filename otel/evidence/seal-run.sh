#!/usr/bin/env bash
# =============================================================================
# seal-run.sh — freeze, hash, encrypt and sign one collected run folder.
#
# Run this ON THE ARCHIVAL HOST/ACCOUNT, never by the agent. It assumes the run
# folder is already frozen (producers stopped, sessions exported, artifacts
# gathered, completeness.json written) and that the GPG keys were created and
# authenticated out of band.
#
# What it proves / does not prove:
#   - PROVES byte integrity of the sealed archive against the signature.
#   - Does NOT prove collection completeness, nor the truth of statements the
#     files contain. A signed inaccurate log is still inaccurate.
#
# Order matters: hash/sign only AFTER the folder stops changing. Signing while
# files mutate yields a state no one can verify.
#
# Usage:
#   ARCHIVE_RECIPIENT=<enc-key-fpr> ARCHIVE_SIGNER=<sign-key-fpr> \
#     ./seal-run.sh /srv/evidence-staging/run-YYYYMMDD-NNN
# =============================================================================
set -euo pipefail
umask 077

RUN_DIR="${1:?usage: seal-run.sh <run_dir>}"
: "${ARCHIVE_RECIPIENT:?set ARCHIVE_RECIPIENT to the encryption key fingerprint}"
: "${ARCHIVE_SIGNER:?set ARCHIVE_SIGNER to the signing key fingerprint}"
command -v gpg >/dev/null || { echo "gpg not found" >&2; exit 127; }
test -d "$RUN_DIR"

RUN_ID="$(basename "$RUN_DIR")"

# 1. Build a per-file manifest BEFORE sealing: path, size, sha256, origin tag.
#    'origin' distinguishes agent declarations from environment observations.
MANIFEST="$RUN_DIR/manifest.json"
{
  echo "{"
  echo "  \"run_id\": \"$RUN_ID\","
  echo "  \"sealed_at_utc\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"files\": ["
  first=1
  while IFS= read -r -d '' f; do
    rel="${f#"$RUN_DIR"/}"
    [ "$rel" = "manifest.json" ] && continue
    size="$(stat -c '%s' "$f")"
    sum="$(sha256sum "$f" | awk '{print $1}')"
    case "$rel" in
      sessions/*)            origin="agent_report" ;;
      events/opencode-harness.log) origin="opencode_harness" ;;
      events/*)              origin="opencode_plugin" ;;
      artifacts/*)           origin="trusted_tool_executor" ;;
      environment/*|inputs/*) origin="operator" ;;
      telemetry/*)           origin="collector_sanitized" ;;
      *)                     origin="operator" ;;
    esac
    [ $first -eq 1 ] || echo ","
    first=0
    printf '    {"path":"%s","size":%s,"sha256":"%s","origin":"%s"}' \
      "$(printf '%s' "$rel" | sed 's/"/\\"/g')" "$size" "$sum" "$origin"
  done < <(find "$RUN_DIR" -type f -print0 | sort -z)
  echo ""
  echo "  ]"
  echo "}"
} > "$MANIFEST"

# 2. Produce an encrypted tarball directly (no cleartext tar on disk).
DEST="$(mktemp -d "/srv/evidence-sealed/$RUN_ID.XXXXXX" 2>/dev/null || mktemp -d)"
tar -C "$RUN_DIR" -cf - . \
  | gpg --output "$DEST/evidence.tar.gpg" --encrypt --recipient "$ARCHIVE_RECIPIENT"

# 3. Hash the ciphertext and sign the hash (detached, armored).
(
  cd "$DEST"
  sha256sum evidence.tar.gpg > SHA256SUMS
  gpg --local-user "$ARCHIVE_SIGNER" --armor --output SHA256SUMS.asc --detach-sign SHA256SUMS
)

echo "[seal] sealed archive: $DEST"
echo "[seal] verify with: gpg --verify $DEST/SHA256SUMS.asc $DEST/SHA256SUMS && (cd $DEST && sha256sum --check --strict SHA256SUMS)"
echo "[seal] confirm the signature is the EXPECTED archival key fingerprint ($ARCHIVE_SIGNER), obtained via a trusted channel."
