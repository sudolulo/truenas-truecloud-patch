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
#      every boot before middlewared starts.
#   2. Applies the patches immediately (no reboot required).
#   3. Restarts middlewared so the backend change takes effect now.

set -euo pipefail

# The directory containing install.sh is the permanent install location.
PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$PATCH_DIR/patch/sitecustomize.py" ]; then
    echo "ERROR: patch files not found at $PATCH_DIR/patch/" >&2
    echo "Run install.sh from a clone of the repository on a persistent pool:" >&2
    echo "  git clone https://github.com/sudolulo/truenas-truecloud-patch \\" >&2
    echo "      /mnt/<pool>/truenas-truecloud-patch" >&2
    echo "  cd /mnt/<pool>/truenas-truecloud-patch && bash install.sh" >&2
    exit 1
fi

echo "=== TrueNAS TrueCloud Provider Patch — Install ==="
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
          "$PATCH_DIR/recover.sh" "$PATCH_DIR/uninstall.sh"
echo "Done."
echo ""

# ── Register PREINIT script ───────────────────────────────────────────────────

echo "Registering PREINIT boot hook ..."

EXISTING_ID=$(midclt call initshutdownscript.query '[]' | \
    python3 -c "
import sys, json
for s in json.load(sys.stdin):
    if s.get('comment') == 'TrueCloud provider patch (S3/B2)':
        print(s['id'])
        break
" 2>/dev/null || true)

if [ -n "$EXISTING_ID" ]; then
    echo "Already registered (id=$EXISTING_ID). Updating path and enabling ..."
    midclt call initshutdownscript.update "$EXISTING_ID" \
        "{\"enabled\": true, \"script\": \"$PATCH_DIR/patch/apply.sh\"}" > /dev/null
else
    midclt call initshutdownscript.create \
        "{\"type\":\"SCRIPT\",\"script\":\"$PATCH_DIR/patch/apply.sh\",\"when\":\"PREINIT\",\"enabled\":true,\"comment\":\"TrueCloud provider patch (S3/B2)\"}" \
        > /dev/null
    echo "Registered."
fi
echo ""

# ── Clear kill switch if set ──────────────────────────────────────────────────

if [ -f "$PATCH_DIR/disabled" ]; then
    rm "$PATCH_DIR/disabled"
    echo "Removed kill switch ($PATCH_DIR/disabled) left from a previous recovery."
    echo ""
fi

# ── Apply now ─────────────────────────────────────────────────────────────────

echo "Applying patches ..."
_log_start=$(wc -c < "$PATCH_DIR/apply.log" 2>/dev/null || echo 0)
bash "$PATCH_DIR/patch/apply.sh"
echo ""
echo "Patch log ($PATCH_DIR/apply.log):"
tail -30 "$PATCH_DIR/apply.log"
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
