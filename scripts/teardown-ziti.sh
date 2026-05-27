#!/usr/bin/env bash
#
# teardown-ziti.sh — Layer C rollback: delete zrok-managed Ziti objects from the
# external production controller at zerotrust.salamahsystems.com:1280.
#
# Pre-staged 2026-05-26 during Phase 2 of the zrok self-hosted rollout.
#
# Strategy:
#   1. Inventory zrok-managed objects (by name prefix + by owner tag) and print.
#   2. Require explicit confirmation typed exactly: I-UNDERSTAND
#   3. Delete in safe reverse-creation order: service-policies → service-edge-router-policy
#      → service → identities. ERPs are NOT touched (we never created one).
#   4. Diff against the preflight snapshot to confirm clean exit.
#
# Safe to re-run: every delete is best-effort (skipped if absent).
#
# Requires: ziti CLI logged in as admin (~/.config/ziti/ziti-cli.json valid).
# Preflight snapshots at ~/zrok-rollout/preflight/*.before.json (copy to laptop
# before Layer B restore if you want diff-on-restore to still work).

set -euo pipefail

PREFLIGHT_DIR="${PREFLIGHT_DIR:-$HOME/zrok-rollout/preflight}"

require_ziti() {
  command -v ziti >/dev/null || { echo "ziti CLI not on PATH"; exit 1; }
  if ! ziti edge list identities 'limit 1' -j >/dev/null 2>&1; then
    echo "Ziti session not valid. Run: ziti edge login zerotrust.salamahsystems.com:1280 -u admin"
    exit 1
  fi
}

inventory() {
  echo "=== Identities with name LIKE 'zrok-%' OR tags.owner=zrok-self-hosted-sg ==="
  ziti edge list identities 'name contains "zrok-" or tags.owner = "zrok-self-hosted-sg" limit 1000' \
    -j 2>/dev/null | jq -r '.data[] | "\(.id)\t\(.name)"'

  echo
  echo "=== Services with name LIKE 'zrok-%' OR tags.owner=zrok-self-hosted-sg ==="
  ziti edge list services 'name contains "zrok-" or tags.owner = "zrok-self-hosted-sg" limit 1000' \
    -j 2>/dev/null | jq -r '.data[] | "\(.id)\t\(.name)"'

  echo
  echo "=== Service-policies (Bind/Dial) with name LIKE 'zrok-%' ==="
  ziti edge list service-policies 'name contains "zrok-" or tags.owner = "zrok-self-hosted-sg" limit 1000' \
    -j 2>/dev/null | jq -r '.data[] | "\(.id)\t\(.name)\t\(.type)"'

  echo
  echo "=== Service-edge-router-policies with name LIKE 'zrok-%' ==="
  ziti edge list service-edge-router-policies 'name contains "zrok-" or tags.owner = "zrok-self-hosted-sg" limit 1000' \
    -j 2>/dev/null | jq -r '.data[] | "\(.id)\t\(.name)"'

  echo
  echo "=== Edge-router-policies with name LIKE 'zrok-%' (should be EMPTY — we never create ERPs) ==="
  ziti edge list edge-router-policies 'name contains "zrok-" or tags.owner = "zrok-self-hosted-sg" limit 1000' \
    -j 2>/dev/null | jq -r '.data[] | "\(.id)\t\(.name)"'
}

confirm() {
  echo
  echo "The objects above will be DELETED from the production Ziti controller."
  echo "Type exactly: I-UNDERSTAND  to proceed."
  read -r reply
  [ "$reply" = "I-UNDERSTAND" ] || { echo "Aborted."; exit 1; }
}

delete_by_kind() {
  local kind="$1"          # e.g. service-policy
  local list_kind="$2"     # e.g. service-policies
  local filter='name contains "zrok-" or tags.owner = "zrok-self-hosted-sg" limit 1000'
  local ids
  ids=$(ziti edge list "$list_kind" "$filter" -j 2>/dev/null | jq -r '.data[].id // empty')
  if [ -z "$ids" ]; then
    echo "  (no $list_kind to delete)"
    return
  fi
  for id in $ids; do
    echo "  deleting $kind $id"
    ziti edge delete "$kind" "$id" || echo "    (delete failed — continuing)"
  done
}

teardown() {
  echo "Tearing down in reverse-creation order..."
  echo "[1/4] service-policies"
  delete_by_kind service-policy service-policies
  echo "[2/4] service-edge-router-policies"
  delete_by_kind service-edge-router-policy service-edge-router-policies
  echo "[3/4] services"
  delete_by_kind service services
  echo "[4/4] identities"
  delete_by_kind identity identities
}

verify() {
  echo
  echo "=== Verification ==="
  if [ -f "$PREFLIGHT_DIR/identities.before.json" ]; then
    echo "Diff against preflight snapshot ($PREFLIGHT_DIR/identities.before.json):"
    diff <(jq -rS '.data[].name' "$PREFLIGHT_DIR/identities.before.json") \
         <(ziti edge list identities -j | jq -rS '.data[].name') || true
    echo
    echo "(empty diff = clean exit; any '> zrok-...' line = stale object remaining)"
  else
    echo "No preflight snapshot at $PREFLIGHT_DIR — cannot diff. Manual inspection only."
  fi
  echo
  echo "Remaining zrok-prefixed objects (should all be empty):"
  for kind in identities services service-policies service-edge-router-policies edge-router-policies; do
    n=$(ziti edge list "$kind" 'name contains "zrok-" or tags.owner = "zrok-self-hosted-sg" limit 1000' \
        -j 2>/dev/null | jq '.data | length')
    echo "  $kind: $n"
  done
}

main() {
  require_ziti
  inventory
  case "${1:-}" in
    --dry-run) echo; echo "Dry-run only. No deletions performed."; exit 0 ;;
    --yes)     ;;
    *)         confirm ;;
  esac
  teardown
  verify
}

main "$@"
