#!/bin/bash
# install.sh — run once on the TrueNAS box to set up truecloud-patch.
#
# Prerequisites: run as root on TrueNAS SCALE with middlewared running.
# Clone this repository to a persistent ZFS pool first:
#
#   git clone https://github.com/sudolulo/truenas-truecloud-patch \
#       /mnt/<pool>/truenas-truecloud-patch
#   cd /mnt/<pool>/truenas-truecloud-patch && bash install.sh
#
# What this does:
#   1. Registers a PREINIT initshutdownscript so patch/apply.sh re-runs on
#      every boot. At boot, apply.sh re-patches the overlay and schedules a
#      one-time deferred middlewared restart to load the patched modules
#      (PREINIT runs after middlewared starts, so a restart is required).
#   2. Applies the patches immediately (no reboot required).
#   3. Restarts middlewared so the backend change takes effect now.

set -euo pipefail

VERSION="0.4.0"

# The directory containing install.sh is the permanent install location.
PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"
_HOOK_COMMENT='TrueCloud provider patch (S3/B2)'
_NESTED_MARKER="$PATCH_DIR/nested_snapshots_enabled"

# ── Options ───────────────────────────────────────────────────────────────────
# Nested-dataset snapshot support is OPT-IN and off by default. It changes how
# backups read their source data, so an unattended re-run (e.g. after a
# `git pull`) must never flip it on or off by itself: with neither flag given,
# whatever was chosen previously is preserved.
_nested_choice=""

usage() {
    cat <<USAGE
Usage: bash install.sh [options]

Options:
  --enable-nested-snapshots   Allow the "Take Snapshot" option on datasets that
                              have child datasets (every pool running Apps).
                              Stock TrueNAS refuses this; see README. Off by
                              default because it changes how backups read data.
  --disable-nested-snapshots  Turn it back off; the stock guard is restored.
  -h, --help                  Show this help.

With neither flag, the current setting is left unchanged.
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --enable-nested-snapshots)  _nested_choice="on" ;;
        --disable-nested-snapshots) _nested_choice="off" ;;
        -h|--help)                  usage; exit 0 ;;
        *)
            echo "ERROR: unknown option: $1" >&2
            echo "" >&2
            usage >&2
            exit 1
            ;;
    esac
    shift
done

if [ ! -f "$PATCH_DIR/patch/apply.sh" ]; then
    echo "ERROR: patch files not found at $PATCH_DIR/patch/" >&2
    echo "Run install.sh from a clone of the repository on a persistent pool:" >&2
    echo "  git clone https://github.com/sudolulo/truenas-truecloud-patch \\" >&2
    echo "      /mnt/<pool>/truenas-truecloud-patch" >&2
    echo "  cd /mnt/<pool>/truenas-truecloud-patch && bash install.sh" >&2
    exit 1
fi

echo "=== TrueNAS TrueCloud Provider Patch v${VERSION} — Install ==="
echo ""

# ── Preflight ─────────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must be run as root." >&2
    exit 1
fi

if ! command -v midclt &>/dev/null; then
    echo "ERROR: midclt not found. Run this script on TrueNAS SCALE." >&2
    exit 1
fi

if ! midclt call core.ping &>/dev/null; then
    echo "ERROR: middlewared is not responding. Is TrueNAS fully booted?" >&2
    exit 1
fi

# ── Set permissions ───────────────────────────────────────────────────────────

echo "Setting permissions ..."
chmod +x "$PATCH_DIR/patch/apply.sh" "$PATCH_DIR/patch/create_task.py" \
          "$PATCH_DIR/recover.sh" "$PATCH_DIR/uninstall.sh" "$PATCH_DIR/update.sh"
echo "Done."
echo ""

# ── Register PREINIT script ───────────────────────────────────────────────────

echo "Registering PREINIT boot hook ..."

EXISTING_ID=$(midclt call initshutdownscript.query '[]' | \
    python3 -c "
import sys, json
for s in json.load(sys.stdin):
    if s.get('comment') == '$_HOOK_COMMENT':
        print(s['id'])
        break
" 2>/dev/null || true)

if [ -n "$EXISTING_ID" ]; then
    echo "Already registered (id=$EXISTING_ID). Updating path, timeout, and enabling ..."
    if ! _midclt_out=$(midclt call initshutdownscript.update "$EXISTING_ID" \
            "{\"enabled\": true, \"script\": \"$PATCH_DIR/patch/apply.sh\", \"timeout\": 120}" 2>&1); then
        echo "ERROR: Failed to update PREINIT hook (id=$EXISTING_ID)." >&2
        [ -n "$_midclt_out" ] && echo "  midclt: $_midclt_out" >&2
        echo "  To remove the stale entry and retry:" >&2
        echo "    midclt call initshutdownscript.delete $EXISTING_ID" >&2
        exit 1
    fi
else
    if ! _midclt_out=$(midclt call initshutdownscript.create \
            "{\"type\":\"SCRIPT\",\"script\":\"$PATCH_DIR/patch/apply.sh\",\"when\":\"PREINIT\",\"enabled\":true,\"timeout\":120,\"comment\":\"$_HOOK_COMMENT\"}" \
            2>&1); then
        echo "ERROR: Failed to register PREINIT hook." >&2
        [ -n "$_midclt_out" ] && echo "  midclt: $_midclt_out" >&2
        exit 1
    fi
    echo "Registered."
fi
echo ""

# ── Clear kill switch if set ──────────────────────────────────────────────────

if [ -f "$PATCH_DIR/disabled" ]; then
    rm "$PATCH_DIR/disabled"
    echo "Removed kill switch ($PATCH_DIR/disabled) left from a previous recovery."
    echo ""
fi

# ── Nested-dataset snapshot support (opt-in) ──────────────────────────────────

case "$_nested_choice" in
    on)
        touch "$_NESTED_MARKER"
        echo "Nested-dataset snapshots: ENABLED"
        echo "  The \"Take Snapshot\" option will be allowed on datasets that have"
        echo "  child datasets. Backups then read from a frozen, complete staging"
        echo "  tree instead of live files."
        echo ""
        echo "  This changes how your backups read their source data. Verify that a"
        echo "  backup completes AND that its restic snapshot actually contains"
        echo "  child-dataset data before you rely on it."
        ;;
    off)
        if [ -f "$_NESTED_MARKER" ]; then
            rm -f "$_NESTED_MARKER"
            # Tear down any staging tree first: those bind mounts PIN their ZFS
            # snapshots, so leaving them would block those snapshots from ever
            # being destroyed. apply.sh (below) then reverts the patched files.
            python3 "$PATCH_DIR/patch/truecloud_nested.py" cleanup || \
                echo "  WARNING: staging mounts remain; unmount them manually."
            echo "Nested-dataset snapshots: DISABLED."
            echo "  apply.sh will revert the patched middleware files and the stock"
            echo "  guard is restored when middlewared restarts (this script does that)."
            echo "  Any task that already has snapshot=true on a nested dataset will"
            echo "  fail validation on its next edit. Turn the option off on those"
            echo "  tasks first, or re-run with --enable-nested-snapshots."
        else
            echo "Nested-dataset snapshots: already disabled."
        fi
        ;;
    *)
        if [ -f "$_NESTED_MARKER" ]; then
            echo "Nested-dataset snapshots: enabled (unchanged)."
        else
            echo "Nested-dataset snapshots: disabled (default)."
            echo "  Enable with: bash install.sh --enable-nested-snapshots"
        fi
        ;;
esac
echo ""

# ── Apply now ─────────────────────────────────────────────────────────────────

echo "Applying patches ..."
_log_start=0
[ -f "$PATCH_DIR/apply.log" ] && _log_start=$(wc -c < "$PATCH_DIR/apply.log")
bash "$PATCH_DIR/patch/apply.sh"
echo ""
echo "Patch log (this run):"
tail -c "+$((_log_start + 1))" "$PATCH_DIR/apply.log" 2>/dev/null || true
echo ""
if tail -c "+$((_log_start + 1))" "$PATCH_DIR/apply.log" 2>/dev/null | grep -qE "WARNING:|ERROR:"; then
    echo "WARNING: apply.sh reported one or more issues — see log above for details."
    echo ""
fi

# ── Restart middlewared ───────────────────────────────────────────────────────

echo "Restarting middlewared so the backend patch takes effect ..."
if ! systemctl restart middlewared; then
    echo "" >&2
    echo "ERROR: middlewared failed to restart." >&2
    echo "  The patch IS installed and will activate automatically on the next boot." >&2
    echo "  To activate now, resolve the issue below and run: systemctl restart middlewared" >&2
    echo "  Check the system log for the root cause:" >&2
    echo "  journalctl -u middlewared -n 50" >&2
    echo "If the problem is unrelated to this patch, recover with:" >&2
    echo "  bash $PATCH_DIR/recover.sh" >&2
    exit 1
fi
echo "Done."
echo ""
echo "Verify the backend patch loaded correctly:"
echo "  python3 $PATCH_DIR/patch/create_task.py verify"
echo ""
echo "Refresh your browser to pick up the UI change."
echo ""
echo "To create a TrueCloud Backup task with S3 or B2 credentials:"
echo "  python3 $PATCH_DIR/patch/create_task.py --help"
