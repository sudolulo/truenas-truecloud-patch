#!/bin/bash
# install.sh — run once on the TrueNAS box to set up truecloud-patch.
#
# Prerequisites: run as root on TrueNAS SCALE with middlewared running.
#
# What this does:
#   1. Copies patch files to /data/truecloud-patch/ (survives OS updates).
#   2. Registers a PREINIT initshutdownscript in the TrueNAS database so
#      apply.sh re-applies the patches on every boot before middlewared starts.
#   3. Applies the patches immediately (no reboot required).
#   4. Restarts middlewared so the backend change takes effect now.

set -euo pipefail

PATCH_DIR="/data/truecloud-patch"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ ! -f "$REPO_DIR/patch/sitecustomize.py" ]; then
    echo "ERROR: patch files not found at $REPO_DIR/patch/" >&2
    echo "Run install.sh from the cloned repository, not via pipe:" >&2
    echo "  git clone https://github.com/sudolulo/truenas-truecloud-patch" >&2
    echo "  cd truenas-truecloud-patch && bash install.sh" >&2
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

# ── Copy files ────────────────────────────────────────────────────────────────

echo "Copying patch files to $PATCH_DIR ..."
mkdir -p "$PATCH_DIR"
cp "$REPO_DIR/patch/sitecustomize.py" "$PATCH_DIR/"
cp "$REPO_DIR/patch/patch_ui.py"      "$PATCH_DIR/"
cp "$REPO_DIR/patch/apply.sh"         "$PATCH_DIR/"
cp "$REPO_DIR/patch/create_task.py"   "$PATCH_DIR/"
cp "$REPO_DIR/recover.sh"             "$PATCH_DIR/"
chmod +x "$PATCH_DIR/apply.sh" "$PATCH_DIR/create_task.py" "$PATCH_DIR/recover.sh"
echo "Done."
echo ""

# ── Register PREINIT script ───────────────────────────────────────────────────

echo "Registering PREINIT boot hook ..."

EXISTING_ID=$(midclt call initshutdownscript.query '[]' | \
    python3 -c "
import sys, json
for s in json.load(sys.stdin):
    if s.get('script') == '/data/truecloud-patch/apply.sh':
        print(s['id'])
        break
" 2>/dev/null || true)

if [ -n "$EXISTING_ID" ]; then
    echo "Already registered (id=$EXISTING_ID). Ensuring it is enabled ..."
    midclt call initshutdownscript.update "$EXISTING_ID" \
        '{"enabled": true}' > /dev/null
else
    midclt call initshutdownscript.create \
        '{"type":"SCRIPT","script":"/data/truecloud-patch/apply.sh","when":"PREINIT","enabled":true,"comment":"TrueCloud provider patch (S3/B2)"}' \
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
bash "$PATCH_DIR/apply.sh"
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
echo "  python3 $PATCH_DIR/create_task.py verify"
echo ""
echo "Refresh your browser to pick up the UI change."
echo ""
echo "To create a TrueCloud Backup task with S3 or B2 credentials:"
echo "  python3 $PATCH_DIR/create_task.py --help"
