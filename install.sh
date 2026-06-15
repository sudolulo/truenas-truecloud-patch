#!/bin/bash
# install.sh — run once on the TrueNAS box to set up the patch.
#
# What this does:
#   1. Copies patch files to /data/truecloud-patch/ (survives updates).
#   2. Registers a PREINIT initshutdownscript so apply.sh runs on every boot
#      before middlewared starts, re-applying patches to the refreshed /usr/.
#   3. Applies the patches immediately without rebooting.
#   4. Restarts middlewared so the backend patch takes effect now.

set -euo pipefail

PATCH_DIR="/data/truecloud-patch"
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== TrueNAS TrueCloud Provider Patch ==="
echo ""

if ! command -v midclt &>/dev/null; then
    echo "ERROR: midclt not found. Run this script on TrueNAS SCALE." >&2
    exit 1
fi

# ── Copy files ────────────────────────────────────────────────────────────────
echo "Copying patch files to $PATCH_DIR ..."
mkdir -p "$PATCH_DIR"
cp "$REPO_DIR/patch/sitecustomize.py" "$PATCH_DIR/"
cp "$REPO_DIR/patch/patch_ui.py"      "$PATCH_DIR/"
cp "$REPO_DIR/patch/apply.sh"         "$PATCH_DIR/"
cp "$REPO_DIR/patch/create_task.py"   "$PATCH_DIR/"
chmod +x "$PATCH_DIR/apply.sh" "$PATCH_DIR/create_task.py"
echo "Done."
echo ""

# ── Register PREINIT script ───────────────────────────────────────────────────
echo "Checking initshutdownscript registration ..."

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
    echo "Registered PREINIT script."
fi
echo ""

# ── Apply now ─────────────────────────────────────────────────────────────────
echo "Applying patches ..."
bash "$PATCH_DIR/apply.sh"
cat "$PATCH_DIR/apply.log" | tail -20
echo ""

# ── Restart middlewared ───────────────────────────────────────────────────────
echo "Restarting middlewared (backend patch takes effect) ..."
systemctl restart middlewared
echo "Done."
echo ""

echo "Refresh your browser to pick up the UI change."
echo ""
echo "To create a TrueCloud Backup task with S3 or B2 credentials:"
echo "  python3 $PATCH_DIR/create_task.py --help"
